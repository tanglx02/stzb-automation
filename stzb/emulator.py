# -*- coding: utf-8 -*-
"""MuMu 模拟器 12 的启停控制。

为什么不用 `MuMuPlayer.exe` 直接双击启动：
  双击只能「打开模拟器」，开完还得自己点游戏图标，脚本没法知道什么时候真的起来。
  MuMu 自带命令行 `MuMuManager.exe`，可以
      control -v <index> launch --package <pkg>    # 起模拟器 + 起游戏，一条命令
      control -v <index> shutdown                  # 关掉这台虚拟机
      info    -v <index>                           # 返回 JSON，含 is_android_started / player_state
  用它才能做到「先看是否已在跑 → 没跑就拉起来 → 轮询等到 Android 真的启动完成」。

实测要点（本机 MuMu 12 + Android 15）：
  · MuMuManager 的输出是 **GBK**，必须按 gbk 解码，否则中文全是乱码。
  · `info` 返回的是 JSON，可以直接解析。
  · 同一台虚拟机会同时以 `127.0.0.1:7555` 和 `emulator-5554` 两个 adb 传输通道出现，
    它们是**同一个** Android（实测 model / sdk 完全一致）。判定「是否已连上」时按候选逐个试即可。
  · `info` 里的 `adb_port` 报的是 16384，和实际在用的 7555 不一致，所以
    **不要拿 adb_port 去拼 serial**，一律用 config 里的 serial_candidates 探测。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Callable, Dict, List, Optional, Sequence

DEFAULT_MANAGER = r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"

# 模拟器的 serial 形态：本地端口（127.0.0.1:7555）或 emulator-5554。
# 用途见 adb_online()：用来把「USB 实体机」挡在模拟器那条路之外。
_EMU_SERIAL_RE = re.compile(r"^(?:127\.0\.0\.1:\d+|localhost:\d+|emulator-\d+)$", re.I)

# 放宽版：**任何**「传输端点」形态（host:port / emulator-N）都算。
# 为什么要比 _EMU_SERIAL_RE 宽：MuMu 的 adb 端口本来是 127.0.0.1:7555，但用户
# 完全可能改了 adb 监听地址（局域网连自己的模拟器），这时 serial 会是
# 192.168.x.x:5555 —— 它仍然是模拟器，不该被当成「陌生实体机」拒掉。
# 判据的核心是**形状**：USB 实体机的 serial（340436524100AJ8）里永远没有冒号。
_EMU_SHAPE_RE = re.compile(r"^(?:emulator-\d+|[A-Za-z0-9._-]+:\d+)$", re.I)

# player_state 到达这个值表示 Android 启动完成
STATE_READY = "start_finished"


class EmulatorError(RuntimeError):
    pass


class MuMu:
    """MuMu 12 虚拟机的启停 + 就绪等待。"""

    def __init__(self, manager: str = DEFAULT_MANAGER, vmindex: int = 0,
                 adb: Optional[str] = None,
                 serial_candidates: Optional[Sequence[str]] = None,
                 logger: Callable[[str], None] = print,
                 startup_timeout: float = 300.0):
        self.manager = manager
        self.vmindex = int(vmindex)
        self.adb = adb
        self.serial_candidates = list(serial_candidates or [])
        self.log = logger
        self.startup_timeout = float(startup_timeout)

    # ---------------------------------------------------------------- 底层调用

    def _run(self, args: Sequence[str], timeout: float = 90.0) -> tuple:
        """跑一条 MuMuManager 命令，返回 (rc, 输出文本)。输出按 GBK 解码。"""
        cmd = [self.manager] + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise EmulatorError("找不到 MuMuManager：%s\n"
                                "请在 config.json 的 emulator.manager 里填对路径。" % self.manager)
        except subprocess.TimeoutExpired:
            return 1, "<超时 %ss>" % timeout
        out = (p.stdout or b"").decode("gbk", "replace").strip()
        err = (p.stderr or b"").decode("gbk", "replace").strip()
        return p.returncode, (out or err)

    def available(self) -> bool:
        return os.path.exists(self.manager)

    # ---------------------------------------------------------------- 状态查询

    def info(self) -> Optional[Dict]:
        """返回这台虚拟机的状态字典；查不到返回 None。"""
        rc, out = self._run(["info", "-v", str(self.vmindex)], timeout=30)
        if rc != 0 or not out:
            return None
        try:
            return json.loads(out)
        except Exception:
            return None

    def is_running(self) -> bool:
        """虚拟机进程在跑（不管 Android 有没有启动完）。"""
        d = self.info()
        return bool(d and d.get("is_process_started"))

    def is_ready(self) -> bool:
        """Android 已经启动完成 —— 只有到这一步 adb 才连得上。"""
        d = self.info()
        if not d:
            return False
        return bool(d.get("is_android_started")) and d.get("player_state") == STATE_READY

    # ---------------------------------------------------------------- adb 侧

    def adb_serials(self) -> List[str]:
        """当前 adb devices 里状态为 device 的序列号。"""
        if not self.adb:
            return []
        try:
            p = subprocess.run([self.adb, "devices"], capture_output=True, timeout=20)
        except Exception:
            return []
        out = p.stdout.decode("utf-8", "replace")
        found = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("List of"):
                continue
            parts = line.split("\t") if "\t" in line else line.split()
            if len(parts) >= 2 and parts[1] == "device":
                found.append(parts[0])
        return found

    def adb_online(self) -> Optional[str]:
        """返回已连上的候选序列号。

        ★ 退路被收窄过（2026-09-21 实测踩坑）：原来最后会「返回任意一个在线设备」，
          于是**插着 USB 实体机时**这个方法会返回手机的 serial（340436524100AJ8），
          调用方（如 start_emulator / run_daily --status）就会以为「模拟器已就绪」，
          实际模拟器根本没起来 —— 是那台手机在冒充。

          所以现在的规则是：候选全不在线时，**只**接受形状像模拟器的 serial
          （见 `_EMU_SHAPE_RE`：`emulator-N` 或 `host:port`），绝不让 USB 实体机
          替模拟器背锅。实体机是设备管理里**另一条**路径（后台点名走 extra.serial），
          跟「模拟器起没起来」是两件事，不能混。
        """
        online = self.adb_serials()
        for c in self.serial_candidates:
            if c in online:
                return c
        for s in online:
            if _EMU_SHAPE_RE.match(s):
                return s
        return None

    def ensure_adb(self) -> Optional[str]:
        """主动 connect 各候选端口，返回可用序列号（语义同 adb_online）。"""
        s = self.adb_online()
        if s:
            return s
        if not self.adb:
            return None
        for c in self.serial_candidates:
            if c.startswith("emulator"):
                continue
            try:
                subprocess.run([self.adb, "connect", c], capture_output=True, timeout=15)
            except Exception:
                pass
        return self.adb_online()

    # ---------------------------------------------------------------- 启停

    def start(self, package: Optional[str] = None, wait: bool = True) -> bool:
        """启动虚拟机。package 非空时顺带把该 App 拉起来（MuMu 的 launch 支持）。

        已启动过就退化成「确保 adb 连上」，不会重复启动。
        """
        if not self.available():
            self.log("!! 找不到 MuMuManager：%s" % self.manager)
            return False

        if self.is_running():
            self.log("  · 模拟器本来就在运行，不重复启动")
        else:
            args = ["control", "-v", str(self.vmindex), "launch"]
            if package:
                args += ["--package", package]
            self.log("  · 启动模拟器（vmindex=%d%s）…"
                     % (self.vmindex, ("，并拉起 %s" % package) if package else ""))
            rc, out = self._run(args, timeout=120)
            if rc != 0:
                self.log("    × MuMuManager launch 返回 %d：%s" % (rc, out[:200]))
                # 退路：直接拉起主程序，它会自动启动默认虚拟机
                self._launch_main_app()
        if wait and not self.wait_ready():
            return False
        return True

    def _launch_main_app(self):
        """退路：主 GUI 程序在时用 Popen 拉起来（不进 PATH，必须绝对路径）。"""
        cand = os.path.join(os.path.dirname(self.manager), "MuMuNxMain.exe")
        if not os.path.exists(cand):
            return
        self.log("    · 退路：直接拉起 %s" % os.path.basename(cand))
        try:
            subprocess.Popen([cand], cwd=os.path.dirname(cand))
        except Exception as e:
            self.log("    × 拉起主程序失败：%r" % e)

    def wait_ready(self, timeout: Optional[float] = None,
                   interval: float = 3.0) -> bool:
        """轮询等到 Android 启动完成 + adb 连上。"""
        timeout = float(timeout or self.startup_timeout)
        end = time.time() + timeout
        t0 = time.time()
        last_note = 0.0
        while time.time() < end:
            if self.is_ready():
                s = self.ensure_adb()
                if s:
                    self.log("  ✓ 模拟器就绪（%.0f 秒），adb=%s" % (time.time() - t0, s))
                    return True
            # 每 15 秒报一次进度，免得看起来像卡死
            if time.time() - last_note >= 15:
                last_note = time.time()
                d = self.info() or {}
                self.log("    · 等待模拟器就绪…（%.0f 秒，state=%s android=%s adb=%s）"
                         % (time.time() - t0,
                            d.get("player_state", "?"),
                            d.get("is_android_started", "?"),
                            self.adb_online() or "无"))
            time.sleep(interval)
        self.log("  × 等了 %.0f 秒模拟器仍没就绪" % timeout)
        return False

    def stop(self, wait: bool = True, timeout: float = 90.0) -> bool:
        """关机（正常关闭虚拟机）。"""
        if not self.is_running():
            self.log("  · 模拟器本来就没在运行")
            return True
        self.log("  · 关闭模拟器…")
        rc, out = self._run(["control", "-v", str(self.vmindex), "shutdown"], timeout=60)
        if rc != 0:
            self.log("    × shutdown 返回 %d：%s" % (rc, out[:200]))
        if not wait:
            return rc == 0
        end = time.time() + timeout
        while time.time() < end:
            if not self.is_running():
                self.log("  ✓ 模拟器已关闭")
                return True
            time.sleep(2)
        self.log("  × 等了 %.0f 秒模拟器还在跑" % timeout)
        return False

    def kill_all(self) -> bool:
        """连主程序一起杀掉 —— 彻底清干净，比 shutdown 更狠。

        实测坑：MuMu 主程序（GUI 窗口）没在跑、只有虚拟机进程时，
        `main kill` **不会**动虚拟机，它会「成功地什么都没做」。
        所以必须补一刀 `control shutdown`，否则会出现「以为关干净了、结果 VM 还在」。
        """
        self.log("  · 彻底关闭 MuMu（主程序 + 全部虚拟机）…")
        self._run(["main", "kill"], timeout=60)
        if self.is_running():
            rc, out = self._run(["control", "-v", str(self.vmindex), "shutdown"], timeout=60)
            if rc != 0:
                self.log("    × shutdown 返回 %d：%s" % (rc, out[:160]))
        deadline = time.time() + 90
        while time.time() < deadline:
            if not self.is_running():
                self.log("  ✓ MuMu 已完全退出")
                return True
            time.sleep(2)
        self.log("  × 等了 90 秒虚拟机仍在运行")
        return False

    def status_line(self) -> str:
        d = self.info()
        if not d:
            return "模拟器：未运行 / 查不到状态"
        return ("模拟器：state=%s android=%s pid=%s adb=%s"
                % (d.get("player_state", "?"),
                   d.get("is_android_started", "?"),
                   d.get("pid", "?"),
                   self.adb_online() or "未连接"))
