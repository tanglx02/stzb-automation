# -*- coding: utf-8 -*-
"""常驻探活代理 —— 在每台客户端机器上挂着跑，让后台随时能看到它「在线」。

**为什么需要单独一个入口：**
`run_daily.py` 只在跑任务那几分钟里发心跳。定时任务是每天两档，
剩下 20 多个小时后台看到的都是「离线」—— 那就失去「探测客户端是否在线」的意义了。
所以这个脚本常驻后台，只做两件事：

  1. 每 30 秒发一次心跳，让后台的「客户端」页一直显示在线
  2. 收后台点的「探测」指令 → 截屏 + OCR → 把界面文字回传

它**不会**自动跑游戏任务（那是 run_daily 的事），除了心跳和探测之外，
不在游戏里做任何点击。想验证界面就点探测，想跑任务就走 run_daily / 待执行队列。

用法：
    python agent.py                 # 前台常驻，日志打到屏幕和 logs/agent.log
    python agent.py --once          # 只发一次心跳就退出（用来快速验证配置）
    python agent.py --no-probe      # 只心跳，不响应探测指令（不想让它碰模拟器时用）
    python agent.py --interval 60   # 覆盖心跳间隔（秒）

配合 Windows 计划任务 / 开机自启：
    pythonw.exe agent.py            # 无窗口常驻
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from stzb.account import describe_screen, ensure_target, find_masked   # noqa: E402
from stzb.cloud import CloudClient, CloudError                          # noqa: E402
from stzb.config import load                                            # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device                     # noqa: E402
from stzb.heartbeat import Heartbeat, StatusBox                         # noqa: E402
from stzb.identity import Identity, read_assignment_cache, \
    write_assignment_cache                                              # noqa: E402
from stzb.ui import Ui                                                  # noqa: E402

CLIENT_STATE = os.path.join(ROOT, "state", "client.json")
ASSIGN_CACHE = os.path.join(ROOT, "state", "assignment.json")
SHOT_DIR = os.path.join(ROOT, "logs", "shots")
LOG_DIR = os.path.join(ROOT, "logs")


class Logger:
    """带时间戳、同时进屏幕和文件。代理要长期跑，必须有文件日志可回溯。"""

    def __init__(self, path: str, quiet: bool = False):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.quiet = quiet
        self.f = io.open(path, "a", encoding="utf-8")

    def __call__(self, msg: str) -> None:
        line = "[%s] %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        if not self.quiet:
            try:
                print(line, flush=True)
            except Exception:
                pass
        try:
            self.f.write(line + "\n")
            self.f.flush()
        except Exception:
            pass


def _apply_cloud_env(cfg) -> None:
    cloud = cfg.setdefault("cloud", {})
    for env, key, cast in (("STZB_CLOUD_BASE_URL", "base_url", str),
                           ("STZB_CLOUD_TOKEN", "token", str),
                           ("STZB_CLOUD_ENABLED", "enabled",
                            lambda v: str(v).strip().lower() in ("1", "true", "yes", "on"))):
        v = os.environ.get(env)
        if v not in (None, ""):
            cloud[key] = cast(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="率土自动化 · 常驻探活代理")
    ap.add_argument("--once", action="store_true", help="只发一次心跳就退出")
    ap.add_argument("--interval", type=int, default=0, help="心跳间隔（秒），默认用服务端的")
    ap.add_argument("--no-probe", action="store_true",
                    help="不响应探测指令（完全不碰模拟器/ADB）")
    ap.add_argument("--quiet", action="store_true", help="不往屏幕打印，只写日志文件")
    args = ap.parse_args()

    os.makedirs(SHOT_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    log = Logger(os.path.join(LOG_DIR, "agent.log"), quiet=args.quiet)

    cfg = load()
    _apply_cloud_env(cfg)
    cloud = cfg.get("cloud") or {}

    identity = Identity.load(CLIENT_STATE)
    log("=" * 60)
    log("常驻探活代理启动")
    log("  客户端标识 : %s" % identity.uid)
    log("  机器名     : %s" % identity.host)

    if not cloud.get("enabled"):
        log("!! config.json 里 cloud.enabled 不是 true，代理没事可做。")
        log("   想让后台看到这台机器在线，请：")
        log("     1. 把 cloud.enabled 改成 true")
        log("     2. 填好 cloud.base_url（后台地址）和 cloud.token（采集端令牌）")
        log("   后台地址与令牌在控制台的「设置」页能看到。")
        return 1
    if not str(cloud.get("base_url") or "").strip() or not str(cloud.get("token") or "").strip():
        log("!! cloud.base_url 或 cloud.token 没填全，代理没事可做。")
        return 1

    try:
        client = CloudClient(base_url=cloud.get("base_url", ""),
                             token=cloud.get("token", ""),
                             timeout=int(cloud.get("timeout", 90)),
                             retries=2, logger=log, uid=identity.uid)
    except CloudError as e:
        log("!! 后端客户端初始化失败：%s" % e)
        return 1

    ok, info = client.ping()
    if not ok:
        log("!! 第一次探活失败：%s" % info)
        log("   检查：地址对不对、令牌对不对、这台机器能不能访问公网。")
        log("   代理会继续重试，网络恢复后会自动上线。")
    else:
        log("  后端连接   : %s ✓" % cloud.get("base_url"))
        log("  心跳间隔   : %s 秒（服务端指定）"
            % (info or {}).get("heartbeat_interval", "?"))
        if (info or {}).get("task_paused"):
            log("  ! 后台把这台客户端设为「暂停派发」")

    if args.once:
        log("按 --once 退出。")
        return 0

    # ---- 探测用的 Ui 是懒建的：不点探测就完全不碰模拟器 ----
    box = StatusBox(mode="managed")
    box.set(state="idle", note="常驻代理在线（未在跑任务）")
    cached = read_assignment_cache(ASSIGN_CACHE)
    if cached:
        box.patch_current(masked=cached.get("masked"), role=cached.get("role"),
                          account_label=cached.get("label"))

    ui_holder: dict = {"ui": None, "tried": 0.0}

    def make_ui():
        """按需连一次 ADB 并造 Ui。连不上就返回 None（代理自己不该因此挂掉）。"""
        now = time.time()
        if ui_holder["ui"] is not None:
            return ui_holder["ui"]
        if now - ui_holder["tried"] < 30:      # 失败后 30 秒内不反复重试
            return None
        ui_holder["tried"] = now
        try:
            dev = Device(adb=cfg.get("device.adb") or DEFAULT_ADB,
                         serial_candidates=cfg.get("device.serial_candidates"),
                         shot_dir=SHOT_DIR)
            if not dev.connect(retries=2, wait=1.0):
                log("  ! 探测时连不上 ADB（模拟器没开？）")
                return None
            box.patch_device(emulator_running=True, adb_serial=dev.serial,
                             game_running=dev.game_running(cfg.get("device.package") or GAME_PKG))
            ui_holder["ui"] = Ui(dev, cfg, logger=log, dry_run=True)
            return ui_holder["ui"]
        except Exception as e:
            log("  ! 创建 Ui 失败：%r" % (e,))
            return None

    on_assignment_called: dict = {}

    def on_assignment(assignment, switch_needed, reason):
        if assignment:
            write_assignment_cache(assignment, ASSIGN_CACHE)
            box.patch_current(masked=assignment.get("masked"),
                              role=assignment.get("role"),
                              account_label=assignment.get("label"))
        if switch_needed:
            on_assignment_called["pending"] = reason
            log("  · 后台要求切换：%s（用后台的「强制切换」指令来真正执行）" % reason)

    def on_command(cmd):
        kind = cmd.get("kind") or "probe"
        note = cmd.get("note") or ""
        log("  · 收到后台指令：%s%s" % (kind, ("（%s）" % note) if note else ""))

        if args.no_probe:
            return False, "本机代理以 --no-probe 启动，不执行探测", {}

        ui = make_ui()
        if ui is None:
            return False, "连不上模拟器/ADB（模拟器可能没开），探测不了", {}

        if kind == "probe":
            info = describe_screen(ui)
            masked = None
            try:
                items, _ = ui.ocr("agent_probe_acct")
                masked = find_masked(items)
            except Exception:
                pass
            if masked:
                box.patch_current(masked=masked)
            msg = "读到 %d 行文字" % info.get("text_count", 0)
            if masked:
                msg += "，当前账号 %s" % masked
            # 顺便把「当前界面像不像主城/登录页」说清楚，方便判断状态
            try:
                items, _ = ui.ocr("agent_probe_state")
                if ui.is_home(items):
                    msg += "，已在主城"
                    box.set(state="idle", note="探测：已在主城")
                elif ui.has(items, "开始游戏", "廾始游戏", "并始游戏"):
                    msg += "，停在登录页"
                    box.set(state="idle", note="探测：停在登录页")
                else:
                    box.set(state="idle", note="探测：%s" % msg)
            except Exception:
                pass
            return True, msg, info

        if kind == "switch":
            target = cached or read_assignment_cache(ASSIGN_CACHE)
            if not target:
                return False, "后台还没给这台客户端指派账号/角色", {}
            r = ensure_target(ui, target, log=log)
            if r.ok:
                box.patch_current(masked=r.masked or target.get("masked"),
                                  role=r.role or target.get("role"),
                                  switched_at=dt.datetime.now().isoformat(timespec="seconds"))
                on_assignment_called.pop("pending", None)
            return r.ok, ("切换成功：" + r.reason) if r.ok else ("切换失败：" + r.reason), \
                r.to_dict()

        return False, "常驻代理不处理指令：%s（跑任务请用 run_daily 或后台的待执行队列）" % kind, {}

    hb = Heartbeat(client, identity, state_fn=box.get,
                   on_assignment=on_assignment, on_command=on_command,
                   logger=log,
                   interval=args.interval or int((info or {}).get("heartbeat_interval") or 30))
    hb.start()
    log("代理已进入常驻状态（心跳每 %s 秒一次）。按 Ctrl+C 退出。" % hb.interval)
    if args.no_probe:
        log("  注意：以 --no-probe 运行，不会响应「探测」「强制切换」指令。")

    try:
        while True:
            time.sleep(1)
            # 每 5 分钟打一条「我还活着」，否则日志会静得让人以为挂了
            if hb.beats and hb.beats % max(1, int(300 / max(1, hb.interval))) == 0:
                pass
    except KeyboardInterrupt:
        log("收到 Ctrl+C，代理退出。")
    finally:
        hb.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())