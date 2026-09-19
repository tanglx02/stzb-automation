# -*- coding: utf-8 -*-
"""以「脱离进程组」的方式启动 MuMu，使其不随调用方 shell 退出而被回收。

背景：在沙箱/子 shell 里跑 `MuMuManager control launch` 时，模拟器进程属于
调用方的进程树；调用方命令一结束，整棵树被回收，模拟器跟着消失
（表现为「刚报就绪，下一个命令再查就 is_process_started=False」）。
解决：用 DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP 起，让它挂到系统上。

用法：
    python tools/detach_emu.py start [vmindex]
    python tools/detach_emu.py stop  [vmindex]
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MGR = r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def launch_detached(args) -> int:
    p = subprocess.Popen(
        [MGR] + list(args),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        close_fds=True)
    return p.pid


def state(vm: int):
    p = subprocess.run([MGR, "info", "-v", str(vm)], capture_output=True, timeout=30)
    out = (p.stdout or b"").decode("gbk", "replace")
    import json
    try:
        return json.loads(out)
    except Exception:
        return {}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    vm = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    if cmd == "start":
        if state(vm).get("is_process_started"):
            print("已在运行")
            return 0
        pid = launch_detached(["control", "-v", str(vm), "launch"])
        print("已脱离启动 MuMuManager pid=%d，等待就绪…" % pid)
        for i in range(60):
            time.sleep(2)
            d = state(vm)
            if d.get("is_android_started") and d.get("player_state") == "start_finished":
                print("✓ 就绪（%d 秒）" % ((i + 1) * 2))
                return 0
        print("× 超时仍未就绪")
        return 1

    if cmd == "stop":
        launch_detached(["control", "-v", str(vm), "shutdown"])
        print("已发送关机命令")
        return 0

    print("未知命令：%s" % cmd)
    return 1


if __name__ == "__main__":
    sys.exit(main())