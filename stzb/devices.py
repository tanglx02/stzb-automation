# -*- coding: utf-8 -*-
"""设备能力：扫描 ADB 在线设备、识别设备类型、实体机横屏适配、远程启停模拟器。

**这个模块为什么存在（2026-09-21 用户需求）：**
用户要求「管理员能从后台直接启动安卓模拟器」并且「后台还能添加 USB 连的实体手机」。
之前项目里 `Device` 类只认一个 serial（`config.json` 里写死的候选端口），
`MuMu` 类只认 vmindex，两者都不支持「后端点名操作某台设备」。

所以把「设备」这个概念独立出来：一台设备 = 一个 ADB serial，它的类型（模拟器/实体机）
由 serial 形态 + 探针推断。后端通过指令的 `extra.serial` 点名要操作哪台。

    from stzb.devices import scan_devices, DeviceInfo, force_landscape

    for d in scan_devices(adb):
        print(d.serial, d.kind, d.model)

设计要点（都是实测得来）：
1. **实体机必须强制横屏**：手机原生 1080×2400（竖屏），游戏横屏后是 2400×1080，
   而项目全部坐标写死 1920×1080。实测 `adb shell wm size 1080x1920` 覆盖后，
   截图变 1920×1080 且**游戏 UI 与模拟器逐像素一致**
   （验证：OCR 找到「批阅」按钮在 (1229,888)，与代码常量 BTN_JUNQING 完全相同）。
2. **覆盖是可逆的**：`wm size reset` 恢复原生。用户拔线前应该恢复，
   否则他手机桌面会变形 —— 所以提供 restore，且在设备被移除时尽量还原。
3. **类型推断优先看 serial 形态**：`127.0.0.1:port` / `emulator-5554` 一定是模拟器；
   一长串字母数字（如 340436524100AJ8）是实体机。再用 `ro.product.model` 佐证。
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Callable, Dict, List, Optional, Sequence

# 模拟器的 serial 形态：本地端口（127.0.0.1:7555）或 emulator-5554
_EMU_SERIAL_RE = re.compile(r"^(?:127\.0\.0\.1:\d+|localhost:\d+|emulator-\d+)$", re.I)


class DeviceInfo:
    """一台 ADB 设备的静态信息。"""

    # 目标横屏覆盖值（wm size 的**竖屏基准**写法）。见 force_landscape 的说明。
    TARGET = "1080x1920"

    __slots__ = ("serial", "kind", "state", "model", "product", "brand",
                 "android", "screen_w", "screen_h", "native_w", "native_h",
                 "override")

    def __init__(self, serial: str, kind: str = "emulator", state: str = "device"):
        self.serial = serial
        self.kind = kind                    # emulator | phone
        self.state = state                  # device / offline / unauthorized
        self.model = ""
        self.product = ""
        self.brand = ""
        self.android = ""
        self.screen_w = 0
        self.screen_h = 0
        self.native_w = 0
        self.native_h = 0
        self.override = ""                  # wm size 报的 override 原样（如 "1080x1920"）

    @property
    def online(self) -> bool:
        return self.state == "device"

    @property
    def kind_label(self) -> str:
        return {"emulator": "安卓模拟器", "phone": "实体手机"}.get(self.kind, self.kind)

    @property
    def needs_landscape(self) -> bool:
        """这台设备**是否还需要**做横屏覆盖（实体机不覆盖就没法用）。

        判据：不是模拟器，且当前没有生效的 1920x1080 覆盖。
        模拟器天然就是 1920x1080 横屏，不需要也不该去覆盖它。
        """
        if self.kind == "emulator":
            return False
        return self.override != self.TARGET

    @property
    def usable(self) -> bool:
        """现在能不能直接拿它跑任务：在线 + 分辨率已对齐 1920x1080。"""
        if not self.online:
            return False
        if self.kind == "emulator":
            return True
        return self.override == self.TARGET

    def to_dict(self) -> Dict:
        return {
            "serial": self.serial, "kind": self.kind, "kind_label": self.kind_label,
            "state": self.state, "online": self.online,
            "model": self.model, "product": self.product, "brand": self.brand,
            "android": self.android,
            "screen_w": self.screen_w, "screen_h": self.screen_h,
            "native_w": self.native_w, "native_h": self.native_h,
            "override": self.override,
            "needs_landscape": self.needs_landscape, "usable": self.usable,
        }

    def describe(self) -> str:
        bits = [self.serial, self.kind_label]
        if self.model:
            bits.append(self.model)
        if self.native_w:
            bits.append("%dx%d" % (self.native_w, self.native_h))
        if self.override:
            bits.append("已覆盖 %s" % self.override)
        return " · ".join(bits)


# ------------------------------------------------------------------ 底层 adb 调用

def _adb_run(adb: str, args: Sequence[str], timeout: float = 20.0) -> str:
    """跑一条 adb 命令，返回 stdout（utf-8 容错解码）。失败返回空串。"""
    try:
        p = subprocess.run([adb] + list(args), capture_output=True, timeout=timeout)
    except Exception:
        return ""
    return (p.stdout or b"").decode("utf-8", "replace")


def list_adb_devices(adb: str) -> List[DeviceInfo]:
    """列 ADB 当前认识的设备（含未授权的，那样才能提示用户去点「允许调试」）。

    **不**依赖 `adb devices -l` 的输出格式细节 —— 只取前两列（serial + state），
    型号另外用 getprop 逐个查。这样 adb 版本差异不会把解析搞崩。
    """
    out = _adb_run(adb, ["devices"], timeout=15.0)
    infos: List[DeviceInfo] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        if line.startswith("*"):            # "adb server is out of date" 之类的提示
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        kind = "emulator" if _EMU_SERIAL_RE.match(serial) else "phone"
        infos.append(DeviceInfo(serial, kind, state))
    return infos


def probe_device(adb: str, info: DeviceInfo, deep: bool = True) -> DeviceInfo:
    """补全一台设备的信息（型号 / 安卓版本 / 屏幕尺寸）。

    deep=False 时只探屏幕尺寸 —— 用于「只想确认能不能用」的快路径。
    """
    if not info.online:
        return info
    s = info.serial

    def sh(cmd: str, t: float = 12.0) -> str:
        return _adb_run(adb, ["-s", s, "shell", cmd], timeout=t).strip()

    if deep:
        info.model = sh("getprop ro.product.model")[:40]
        info.product = sh("getprop ro.product.name")[:40]
        info.brand = sh("getprop ro.product.brand")[:40]
        info.android = sh("getprop ro.build.version.release")[:12]
        # 型号能佐证类型：模拟器型号常带 SDK / Android / emulator 字样
        blob = ("%s %s %s" % (info.model, info.product, info.brand)).lower()
        if any(k in blob for k in ("sdk", "emulator", "virtual", "mumu", "nox",
                                   "ldplayer", "bluestacks", "android sdk")):
            info.kind = "emulator"
        elif info.model and not _EMU_SERIAL_RE.match(s):
            info.kind = "phone"

    # 屏幕：Physical size 是物理分辨率；Override size 出现说明已被覆盖
    #
    # ⚠ 关键语义（实测踩过）：`wm size` 输出的 Override 是**竖屏基准**写法。
    #   在手机上下发 `wm size 1080x1920` 后：
    #       Physical size: 1080x2400
    #       Override size: 1080x1920      ← 原样回显，宽高**没有**互换
    #   但游戏横屏后 `screencap` 出来的图是 **1920x1080**（宽高互换）。
    #   所以「覆盖生效了没」的判据是 **override 值 == 我们要的目标串**，
    #   绝不能用「宽 > 高」去猜 —— 那样会把生效的覆盖误判成失败。
    raw = sh("wm size")
    override = ""
    for line in raw.splitlines():
        line = line.strip()
        m = re.search(r"(\d+)\s*x\s*(\d+)", line)
        if not m:
            continue
        w, h = int(m.group(1)), int(m.group(2))
        if line.lower().startswith("physical"):
            info.native_w, info.native_h = w, h
        elif line.lower().startswith("override"):
            override = "%dx%d" % (w, h)
            info.screen_w, info.screen_h = w, h
    info.override = override
    if not info.screen_w:
        info.screen_w, info.screen_h = info.native_w, info.native_h
    return info


def scan_devices(adb: str, deep: bool = True,
                 logger: Optional[Callable[[str], None]] = None) -> List[Dict]:
    """扫描在线 ADB 设备，返回可直接喂给后端的 dict 列表。"""
    log = logger or (lambda m: None)
    infos = list_adb_devices(adb)
    out = []
    for info in infos:
        if info.online:
            try:
                probe_device(adb, info, deep=deep)
            except Exception as e:
                log("  ! 读取设备 %s 信息失败：%r" % (info.serial, e))
        out.append(info.to_dict())
    log("  · 扫描到 %d 台设备（在线 %d）"
        % (len(out), sum(1 for x in out if x["online"])))
    return out


# ------------------------------------------------------------------ 横屏适配

def force_landscape(adb: str, serial: str, target: str = "",
                    logger: Optional[Callable[[str], None]] = None) -> Dict:
    """把设备的**逻辑分辨率**覆盖成横屏 1920×1080。

    ★ 为什么必须做：项目所有坐标写死 1920×1080。实体机原生 1080×2400 竖屏，
      游戏横屏后截图是 2400×1080 —— 宽度不同，所有 x 坐标都会偏移。
      实测覆盖后（`wm size 1080x1920`）截图变成 1920×1080，
      游戏 UI 与模拟器**逐像素一致**（「批阅」按钮都在 (1229,888)）。

    ⚠ 参数是**竖屏基准**写法：传 "1080x1920" 而不是 "1920x1080"。
      `wm size` 会原样回显 Override size: 1080x1920，
      但 screencap 出来是 1920x1080（系统旋转时宽高互换）。
      所以判定「生效了没」必须比 **override 字符串**，不能比宽高大小。

    返回 {ok, message, override, native_w, native_h, changed}。
    """
    log = logger or (lambda m: None)
    tgt = (target or DeviceInfo.TARGET).strip()
    s = serial
    dev = DeviceInfo(s)
    probe_device(adb, dev, deep=False)
    if not dev.online:
        return {"ok": False, "message": "设备 %s 不在线" % s,
                "override": dev.override, "changed": False}

    if dev.override == tgt:
        return {"ok": True, "message": "已经是 %s，无需覆盖" % tgt,
                "override": dev.override, "native_w": dev.native_w,
                "native_h": dev.native_h, "changed": False}

    out = _adb_run(adb, ["-s", s, "shell", "wm", "size", tgt], timeout=20).strip()
    if out and ("error" in out.lower() or "exception" in out.lower()):
        return {"ok": False, "message": "wm size 失败：%s" % out[:160],
                "override": dev.override, "changed": False}

    probe_device(adb, dev, deep=False)
    ok = (dev.override == tgt)
    if ok:
        log("  · 设备 %s 分辨率已覆盖为 %s（截图 1920x1080）" % (s, tgt))
    else:
        log("  ! 设备 %s 覆盖未生效：override=%s" % (s, dev.override or "(无)"))
    return {"ok": ok,
            "message": ("已覆盖为 %s（截图 1920x1080）" % tgt if ok else
                        "覆盖未生效，当前 override=%s" % (dev.override or "无")),
            "override": dev.override,
            "native_w": dev.native_w, "native_h": dev.native_h,
            "changed": True}


def restore_size(adb: str, serial: str,
                 logger: Optional[Callable[[str], None]] = None) -> Dict:
    """恢复设备原生分辨率（`wm size reset`）。拔线/长期不用时该还原，否则手机桌面会变形。"""
    log = logger or (lambda m: None)
    _adb_run(adb, ["-s", serial, "shell", "wm", "size", "reset"], timeout=20)
    dev = DeviceInfo(serial)
    probe_device(adb, dev, deep=False)
    log("  · 设备 %s 分辨率已恢复原生 %dx%d（override=%s）"
        % (serial, dev.native_w, dev.native_h, dev.override or "无"))
    return {"ok": True, "message": "已恢复原生分辨率",
            "override": dev.override,
            "native_w": dev.native_w, "native_h": dev.native_h}


# ------------------------------------------------------------------ 亮屏 / 防息屏

# `stay_on_while_plugged_in` 的位含义（Android 官方）：AC=1 USB=2 无线=4
STAY_ON_USB = 2


def screen_state(adb: str, serial: str) -> str:
    """返回 'awake' | 'asleep' | 'unknown'。

    读 `dumpsys power` 的 `mWakefulness=`（Android 5+ 全都有）。
    读不到就返回 unknown —— **绝不猜**，猜错的代价见 ensure_awake 的说明。
    """
    out = _adb_run(adb, ["-s", serial, "shell", "dumpsys", "power"], timeout=15.0)
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("mWakefulness="):
            v = s.split("=", 1)[1].strip()
            if v in ("Awake", "Dreaming"):
                return "awake"
            if v in ("Asleep", "Dozing"):
                return "asleep"
    return "unknown"


def screen_locked(adb: str, serial: str) -> Optional[bool]:
    """锁屏（需要密码/图案才能进）了吗？读不到返回 None。

    判据（2026-09-21 在 vivo V2055A / Android 11 实测挑出来的）：
      首选 `dumpsys window policy` 里 **KeyguardStateMonitor 段下的 `mIsShowing`**
      —— 这是系统自己维护的「锁屏在不在显示」，最权威。

    ⚠ 为什么不用 `mCurrentFocus`：实测 `dumpsys window` 里 **有两行
      `mCurrentFocus=`**（第 151 行是 `null`，第 253 行才是真窗口）。
      只取第一行会永远读到 null → 判成「没锁屏」→ 于是在锁屏上瞎点。
      要用它就必须取**最后一行**，而且各家 UI 的类名不统一。
      所以只把它当兜底，且必须取最后一行。

    **宁可返回 None（未知）也不猜 False** —— 把锁屏误判成已解锁，
    脚本就会在锁屏界面上点一通，然后在完全无关的地方报「进不去某界面」。
    """
    policy = _adb_run(adb, ["-s", serial, "shell", "dumpsys", "window", "policy"],
                      timeout=20.0)
    if policy:
        lines = policy.splitlines()
        for i, line in enumerate(lines):
            if "KeyguardStateMonitor" in line:
                # 只看它后面紧跟的十几行，避免吃到别的段里的同名字段
                for nxt in lines[i + 1:i + 15]:
                    s = nxt.strip()
                    if s.startswith("mIsShowing="):
                        return s.split("=", 1)[1].strip().lower().startswith("true")
                break

    win = _adb_run(adb, ["-s", serial, "shell", "dumpsys", "window"], timeout=20.0)
    if win:
        focuses = [ln.strip() for ln in win.splitlines()
                   if ln.strip().startswith("mCurrentFocus=")]
        if focuses:
            return "Keyguard" in focuses[-1]      # ★ 取最后一行，不是第一行
    return None


def ensure_awake(adb: str, serial: str,
                 logger: Optional[Callable[[str], None]] = None) -> Dict:
    """让实体机「亮着 + 跑任务期间不会自己睡着」。

    ★ 为什么必须有（2026-09-21 补全，模拟器时代一直没暴露）：
      模拟器的屏幕永远不会睡，所以这条路径在模拟器上从来没被需要过。
      但**实体手机在任务中途息屏** → `screencap` 拿到的是锁屏/黑屏画面 →
      OCR 一行文字都读不出来 → 表现成「界面认不出来」。而它**不报错**：
      脚本只是不断重试/乱试，最后报一个跟真实原因毫不相干的「进不去某界面」。
      这是最难查的一类现象，所以必须在跑之前主动防住。

    做三件事（都对「插着 USB」这一场景生效，拔线即恢复日常行为）：
      ① 屏幕已经睡着 → 发一次 KEYCODE_WAKEUP 唤醒；
      ② `stay_on_while_plugged_in` 缺 USB 位 → 补上，并**把原值带回去**
         （调用方跑完要还原，见 restore_stay_on —— 别擅自改用户设置);
      ③ 读锁屏状态。真锁着就 `ok=False` 如实报告：**我们没有能力、
         也不应该去猜用户的锁屏密码**，猜等于在别人的手机上乱试密码。

    返回 {ok, message, state, locked, woke, stay_on_before, stay_on_after, changed}
    """
    log = logger or (lambda m: None)
    res = {"ok": False, "message": "", "state": "unknown", "locked": None,
           "woke": False, "stay_on_before": None, "stay_on_after": None,
           "changed": False}

    st = screen_state(adb, serial)
    res["state"] = st
    if st == "asleep":
        _adb_run(adb, ["-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"],
                 timeout=15.0)
        time.sleep(1.2)
        st2 = screen_state(adb, serial)
        res["state"] = st2
        res["woke"] = (st2 == "awake")
        log("  · 设备 %s 原本息屏 → 发了一次唤醒%s"
            % (serial, "，已亮" if res["woke"] else "，仍未亮（可能真的黑屏/关机）"))
        if not res["woke"] and st2 != "unknown":
            res["message"] = "屏幕唤不醒（息屏且 KEYCODE_WAKEUP 无效）"
            return res

    locked = screen_locked(adb, serial)
    res["locked"] = locked
    if locked is True:
        # 先试一次「解开无密码锁屏」——MENU 键（82）是系统标准做法，
        # **它不是猜密码**：手机上真设了 PIN/图案/密码，这一下不会有任何作用，
        # 下面重读仍然是 True，于是照旧拒绝。实测 vivo V2055A 只有滑动锁时，
        # 这一下能把锁屏解开（比 swipe 手势可靠得多 —— 手势坐标会随旋转变）。
        _adb_run(adb, ["-s", serial, "shell", "input", "keyevent", "82"], timeout=15.0)
        time.sleep(1.5)
        locked = screen_locked(adb, serial)
        res["locked"] = locked
        if locked is not True:
            log("  · 设备 %s 的锁屏是无密码滑动锁 → 已用 MENU 键解开" % serial)
    if res["locked"] is True:
        res["message"] = ("设备 %s 处于**锁屏**状态（有密码/图案，脚本无法也不该去解锁）："
                          "请先在手机上解锁，再重跑" % serial)
        log("  ! %s" % res["message"])
        return res

    # 防息屏：只补 USB 位，别把用户其它设置一起改掉
    raw = _adb_run(adb, ["-s", serial, "shell", "settings", "get", "global",
                         "stay_on_while_plugged_in"], timeout=15.0).strip()
    try:
        cur = int(raw)
    except (TypeError, ValueError):
        cur = None
    res["stay_on_before"] = cur
    if cur is not None and not (cur & STAY_ON_USB):
        new = cur | STAY_ON_USB
        _adb_run(adb, ["-s", serial, "shell", "settings", "put", "global",
                       "stay_on_while_plugged_in", str(new)], timeout=15.0)
        res["stay_on_after"] = new
        res["changed"] = True
        log("  · 设备 %s 插电时不常亮（原值 %s）→ 临时设为 %s，跑完还原"
            % (serial, cur, new))
    else:
        res["stay_on_after"] = cur

    res["ok"] = True
    res["message"] = "屏幕就绪" + ("（已唤醒）" if res["woke"] else "")
    return res


def restore_stay_on(adb: str, serial: str, value: Optional[int],
                    logger: Optional[Callable[[str], None]] = None) -> None:
    """把 `stay_on_while_plugged_in` 还原成本次运行之前的值。

    只在 ensure_awake 真的改过它时才需要调用（changed=True）。
    还原失败不抛异常 —— 这只是把用户设置放回去，不该影响收尾链。
    """
    if value is None:
        return
    log = logger or (lambda m: None)
    _adb_run(adb, ["-s", serial, "shell", "settings", "put", "global",
                   "stay_on_while_plugged_in", str(int(value))], timeout=15.0)
    log("  · 设备 %s 的插电常亮设置已还原为 %s" % (serial, value))


def frame_size(adb: str, serial: str
               ) -> Optional[tuple]:
    """截一帧、读 PNG 头，返回**实际画面**的 (宽, 高)；读不到返回 None。

    ★ 为什么不能用 `wm size` 代替（2026-09-21 真机踩到，是真 bug）：
      `wm size 1080x1920` 的 override 字符串**不随物理方向变化**。
      手机竖着时截图是 `1080×1920`，横着时才是 `1920×1080`，
      而 `wm size` 这两种情况下**都回显 `Override size: 1080x1920`**。

      于是「只比 override 字符串」的检查在**竖屏时也会通过** →
      游戏按竖屏重新排版 → 项目里写死的 1920×1080 坐标全部错位 →
      脚本在错误的像素上点满一整轮，而日志看起来完全正常
      （只会看到一堆「认不出的界面」），是最难查的一类问题。

      **唯一可信的判据是截出来的那一帧本身**。一次截屏几 MB，
      但每轮只查一次，换来的是「绝不点错」，非常值。
    """
    try:
        p = subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=60)
        data = p.stdout or b""
    except Exception:
        return None
    # PNG: 8 字节签名 + 4 字节长度 + "IHDR" + 4 字节宽 + 4 字节高（大端）
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    if w <= 0 or h <= 0:
        return None
    return (w, h)


# 项目所有坐标都按这个画面尺寸写死。别的尺寸一律不能跑。
FRAME_W, FRAME_H = 1920, 1080

# 能算出「1920×1080 画面」的 override 候选，按尝试顺序排。
#
# ★★ 这里推翻了老笔记里的一条「铁律」（2026-09-21 真机实测修正）：
#   老笔记写「必须传竖屏基准 1080x1920，不能传 1920x1080」。
#   那只在**手机物理上是横着的**时候成立 —— 因为 `wm size` 是**原样**设置
#   逻辑尺寸，而 screencap 出来的画面会跟着系统旋转再换一次宽高：
#     物理横屏(rotation 1) + override 1080x1920 → 画面 1920x1080  ✅
#     物理竖屏(rotation 0) + override 1080x1920 → 画面 1080x1920  ❌（游戏按竖屏排版）
#     物理竖屏(rotation 0) + override 1920x1080 → 画面 1920x1080  ✅
#   证据：同一台 vivo V2055A，竖屏放着时 `wm size 1920x1080` 直接给出
#   1920×1080 的画面（实测）。
#
#   而「当前是哪个旋转」既不稳定（用户随手一放就变）、又**没法从 ADB 读准**
#   （vivo/OriginOS 忽略 ADB 设的旋转锁，实测连桌面都不跟着转）。
#   → 所以不去猜旋转，**两个候选都试，留那个真能出 1920×1080 画面的**。
#   「实测画面」永远是唯一可信的判据。
OVERRIDE_CANDIDATES = ("1080x1920", "1920x1080")


def _current_override(adb: str, serial: str) -> str:
    """当前 `wm size` 的 override 值（没设过就返回空串）。

    用来在**尝试失败时把它还原回去** —— 见 ensure_frame_1920x1080 里的说明。
    """
    out = _adb_run(adb, ["-s", serial, "shell", "wm", "size"], timeout=15.0) or ""
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("override size:"):
            return s.split(":", 1)[1].strip()
    return ""


def _auto_rotate_on(adb: str, serial: str) -> Optional[bool]:
    """系统的「自动旋转」开着吗？读不到返回 None。

    为什么值得单独读：手机横过来却仍然竖屏排版的常见原因就一个 ——
    **自动旋转被关掉了**（游戏 activity 是 UNSPECIFIED，跟着系统走）。
    这时用户「已经把手机横过来了」但画面还是竖的，光看画面判据会一直失败，
    提示里必须点破这一条，否则用户不知道下一步该动哪里。
    """
    out = _adb_run(adb, ["-s", serial, "shell", "settings", "get",
                         "system", "accelerometer_rotation"], timeout=10.0)
    s = (out or "").strip()
    if s in ("0", "1"):
        return s == "1"
    return None


def ensure_frame_1920x1080(adb: str, serial: str,
                           logger: Optional[Callable[[str], None]] = None) -> Dict:
    """确保**实际画面**就是 1920×1080（项目坐标的前提）。

    做法：逐个试 `OVERRIDE_CANDIDATES`，每试一个就**截一帧验尺寸**
    （不是看 `wm size` 回显 —— 那个字符串不随旋转变化，见 `frame_size` 的说明）。
    取第一个真正给出 1920×1080 的。

    返回 {ok, w, h, override, message}。全都试不通时 ok=False，
    由调用方**拒绝跑**（绝不在错分辨率上点）。
    """
    log = logger or (lambda m: None)

    def _try(target: str):
        out = _adb_run(adb, ["-s", serial, "shell", "wm", "size", target],
                       timeout=20.0).strip()
        if out and ("error" in out.lower() or "exception" in out.lower()):
            return None
        time.sleep(1.2)                       # 等显示重新配置
        return frame_size(adb, serial)

    # 先看现状：可能已经是对的了，那就一个字节都不改
    cur = frame_size(adb, serial)
    if cur == (FRAME_W, FRAME_H):
        return {"ok": True, "w": FRAME_W, "h": FRAME_H, "override": "",
                "message": "画面已经是 1920×1080，无需调整"}

    # 记下进来时的 override，失败要还原（下面每个候选都真的写过一次 wm size）
    before = _current_override(adb, serial)

    probed = []
    for target in OVERRIDE_CANDIDATES:
        sz = _try(target)
        probed.append((target, sz))
        if sz == (FRAME_W, FRAME_H):
            log("  · 设备 %s 覆盖为 %s → 画面 1920×1080 ✓" % (serial, target))
            return {"ok": True, "w": FRAME_W, "h": FRAME_H, "override": target,
                    "message": "已覆盖为 %s（画面 1920×1080）" % target}

    # ★ 都试不通 → 还原 override，**不把用户的手机留在「尺寸被改过」的状态**。
    #   这段的每一步都真的写过 `wm size`，不还原就会停在最后一个候选上；
    #   手机拔下来自己用时界面尺寸会变得莫名其妙，而用户不知道是谁改的。
    #   （方向本来就不对这件事另说 —— 那是调不好的，见下面的提示。）
    try:
        _adb_run(adb, ["-s", serial, "shell", "wm", "size",
                       before or "reset"], timeout=20.0)
    except Exception:                            # noqa: BLE001
        pass

    # 提示要能落地：自动旋转关着是「手机横过来也没用」的唯一原因，必须点破
    ar = _auto_rotate_on(adb, serial)
    if ar is False:
        hint = ("★ 手机的**「自动旋转」是关闭的**：先去设置里把它打开，"
                "再把手机横过来放平（自动旋转关着时，横过来也不会变）")
    elif ar is True:
        hint = ("把手机**横过来放平**再重跑 —— 注意别平放在桌面上，"
                "要有明确的横持姿态，重力感应才判得出来")
    else:
        hint = "把手机横过来放平，并确认系统「自动旋转」是开着的"

    detail = "；".join("%s→%s" % (t, ("%dx%d" % s) if s else "读不到")
                      for t, s in probed)
    return {"ok": False, "w": (cur or (0, 0))[0], "h": (cur or (0, 0))[1],
            "override": "",
            "message": ("试过所有横屏覆盖都拿不到 1920×1080 的画面（%s）。%s。"
                        "（vivo 等 ROM 会忽略 ADB 设的旋转锁，所以脚本没法替你转屏；"
                        "项目坐标写死 1920×1080，画面不对时所有点击都会错位）"
                        % (detail, hint))}


def ensure_landscape_frame(adb: str, serial: str,
                           logger: Optional[Callable[[str], None]] = None) -> Dict:
    """确认**实际画面**就是 1920×1080（横屏）—— 跑任务前的最后一道门。

    返回 {ok, w, h, message}。

    ⚠ 为什么不能「帮用户转一下屏」：实测 vivo（OriginOS）**忽略 ADB 设的旋转锁**
      （`settings put system accelerometer_rotation 0` + `user_rotation 1`
      之后 `mDisplayRotation` 仍是 ROTATION_0，连桌面都不转）。
      所以这里**不去改旋转设置**（改了没用，还会把用户的手机留在
      「旋转被锁住」的状态里），而是在不满足时**如实拒绝并给出可操作的提示**。
      相比「静默乱点一整轮」，宁可让它跑不起来。
    """
    log = logger or (lambda m: None)
    sz = frame_size(adb, serial)
    if sz is None:
        return {"ok": False, "w": None, "h": None,
                "message": "截不到画面（screencap 失败），无法确认分辨率 —— 拒绝盲点"}
    w, h = sz
    if (w, h) == (FRAME_W, FRAME_H):
        return {"ok": True, "w": w, "h": h, "message": "画面 %dx%d，横屏正常" % (w, h)}
    if h > w:
        return {"ok": False, "w": w, "h": h,
                "message": ("手机当前是**竖屏**（画面 %dx%d）：请把手机横过来放平、"
                            "并确认系统「自动旋转」是开着的，再重跑。"
                            "（项目坐标写死 1920×1080，竖屏下所有点击都会错位）"
                            % (w, h))}
    return {"ok": False, "w": w, "h": h,
            "message": ("画面是 %dx%d，不是 1920×1080 —— 拒绝在错误分辨率上点击"
                        % (w, h))}


# ------------------------------------------------------------------ 远程启停模拟器

def start_emulator(manager: str, vmindex: int = 0, package: str = "",
                   adb: str = "", serial_candidates: Optional[Sequence[str]] = None,
                   timeout: float = 300.0,
                   logger: Optional[Callable[[str], None]] = None) -> Dict:
    """远程启动模拟器（复用 stzb.emulator.MuMu，不重复实现）。

    返回 {ok, message, serial}。启动成功后尽力回读 ADB serial，
    这样后端能立刻知道「该用哪个 serial 去连」。
    """
    from .emulator import MuMu, EmulatorError
    log = logger or (lambda m: None)
    emu = MuMu(manager=manager, vmindex=int(vmindex or 0), adb=adb or None,
               serial_candidates=serial_candidates, logger=log,
               startup_timeout=float(timeout))
    if not emu.available():
        return {"ok": False, "message": "找不到 MuMuManager：%s" % manager}
    try:
        ok = emu.start(package=package or None, wait=True)
    except EmulatorError as e:
        return {"ok": False, "message": str(e)[:300]}
    except Exception as e:
        return {"ok": False, "message": "启动异常：%r" % (e,)}

    serial = ""
    try:
        serial = emu.ensure_adb() or ""
    except Exception:
        pass
    return {"ok": bool(ok),
            "message": ("模拟器已启动并就绪" if ok else "模拟器启动超时或失败"),
            "serial": serial}


def stop_emulator(manager: str, vmindex: int = 0,
                  logger: Optional[Callable[[str], None]] = None) -> Dict:
    """远程关闭模拟器。"""
    from .emulator import MuMu, EmulatorError
    log = logger or (lambda m: None)
    emu = MuMu(manager=manager, vmindex=int(vmindex or 0), logger=log)
    if not emu.available():
        return {"ok": False, "message": "找不到 MuMuManager：%s" % manager}
    try:
        ok = emu.stop()
    except EmulatorError as e:
        return {"ok": False, "message": str(e)[:300]}
    except Exception as e:
        return {"ok": False, "message": "关闭异常：%r" % (e,)}
    return {"ok": bool(ok), "message": "模拟器已关闭" if ok else "关闭模拟器失败"}


def emulator_status(manager: str, vmindex: int = 0,
                    logger: Optional[Callable[[str], None]] = None) -> Dict:
    """查模拟器状态（供「扫描设备」时一并汇报）。"""
    from .emulator import MuMu
    log = logger or (lambda m: None)
    emu = MuMu(manager=manager, vmindex=int(vmindex or 0), logger=log)
    if not emu.available():
        return {"available": False, "running": False, "message": "找不到 MuMuManager"}
    try:
        running = bool(emu.is_running())
        ready = bool(emu.is_ready()) if running else False
        return {"available": True, "running": running, "ready": ready,
                "vmindex": int(vmindex)}
    except Exception as e:
        return {"available": True, "running": False, "message": "%r" % (e,)}