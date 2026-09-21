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