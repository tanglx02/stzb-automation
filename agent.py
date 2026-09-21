# -*- coding: utf-8 -*-
"""常驻代理 —— 挂在客户端机器上，**一直和后端保持连接**。

**为什么需要单独一个入口：**
`run_daily.py` 跑完就退出，只在那几分钟里连着后端。定时任务是每天两档，
剩下 20 多个小时后台看不到它，也**推不动它** —— 管理员点了「立刻跑一轮」
也只能干等下一档。所以这个脚本常驻后台，做三件事：

  1. 维持和后端的**实时长连接**（WebSocket；连不上自动退回心跳）
  2. 收后台点的「探测 / 强制切换」指令 → 立刻执行 → 把结果回传
  3. 后台派了任务 → **立刻在本机起一轮 run_daily** 把那批任务跑掉

第 3 条是「后台随时指定任务」能真正落地的关键：任务不是等下一档，
而是派下来几秒内就开始跑。

它自己**不碰游戏界面**（探测除外，那是只读的截屏 + OCR）——
真正的点击全部交给 run_daily 子进程，因为那里有单实例锁和完整的报告逻辑。

用法：
    python agent.py                 # 前台常驻，日志打到屏幕和 logs/agent.log
    python agent.py --once          # 只探活一次就退出（用来快速验证配置）
    python agent.py --no-probe      # 不响应探测指令（完全不碰模拟器/ADB）
    python agent.py --no-run        # 只连不跑：后台派了任务也不自动起一轮
    python agent.py --interval 60   # 覆盖心跳间隔（秒），实时通道不受它影响

配合 Windows 计划任务 / 开机自启：
    pythonw.exe agent.py            # 无窗口常驻
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from stzb.account import describe_screen, ensure_target, find_masked   # noqa: E402
from stzb.cloud import CloudClient, CloudError                          # noqa: E402
from stzb.config import load                                            # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device                     # noqa: E402
from stzb.heartbeat import StatusBox                                     # noqa: E402
from stzb.identity import (Identity, read_assignment_cache,              # noqa: E402
                           run_in_progress, write_assignment_cache)
from stzb.realtime import BUSY_PREFIX, Realtime                          # noqa: E402
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
                           ("STZB_CLOUD_TOKEN", "token", str)):
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
    ap.add_argument("--no-run", action="store_true",
                    help="后台派了任务也不自动起一轮（只连不跑，排障时用）")
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

    # ★ 绑定判据只看 base_url + token 齐不齐，**不看 `cloud.enabled`**。
    #   那个开关 2026-09-20 随「独立模式」一起移除了；老配置里可能还残留
    #   `enabled`，但一律忽略（run_daily.py 里也是同一口径，此处曾经漏改，
    #   后果是 config.json 没有（或 enabled=false）时代理直接退出、
    #   后台永远看不到这台机器在线 —— 会在页面上表现得像"客户端掉线了"，
    #   极难定位，所以两处必须保持同一套判据。）
    if not str(cloud.get("base_url") or "").strip() or not str(cloud.get("token") or "").strip():
        log("!! cloud.base_url 或 cloud.token 没填全，代理没事可做。")
        log("   想让后台看到这台机器在线，请在 config_tool.bat 的「后端托管」里")
        log("   绑定 / 更换服务器（会先验证连通再写入）。也可用环境变量临时覆盖：")
        log("     STZB_CLOUD_BASE_URL / STZB_CLOUD_TOKEN")
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

    def make_ui(serial: str = ""):
        """按需连一次 ADB 并造 Ui。连不上就返回 None（代理自己不该因此挂掉）。

        `serial` 非空 = 后台点名要操作**这一台**设备（多设备场景的关键）：
        此时不走缓存、不去猜候选端口，直接连它。
        """
        if serial:
            try:
                dev = Device(adb=cfg.get("device.adb") or DEFAULT_ADB,
                             serial=serial, serial_candidates=[serial],
                             shot_dir=SHOT_DIR)
                if not dev.connect(retries=2, wait=1.0, serial=serial):
                    log("  ! 连不上后台指定的设备：%s" % serial)
                    return None
                box.patch_device(emulator_running=True, adb_serial=dev.serial,
                                 game_running=dev.game_running(
                                     cfg.get("device.package") or GAME_PKG))
                return Ui(dev, cfg, logger=log, dry_run=True)
            except Exception as e:
                log("  ! 按 serial 建 Ui 失败：%r" % (e,))
                return None

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

    # ---------------------------------------------------------------- 设备类指令
    #
    # 这一组是 2026-09-21 用户需求「后台管理客户端设备」落地的地方。
    # 共同点：**都不需要 ui 对象**（要么是 adb 级操作，要么是启停进程），
    # 所以必须在 `ui = make_ui()` 之前分派 —— 否则「模拟器没开」这个
    # 最需要被处理的场景反而会先因为「连不上 ui」而失败。

    def _dev_adb() -> str:
        return cfg.get("device.adb") or DEFAULT_ADB

    def _dev_manager() -> str:
        from stzb.emulator import DEFAULT_MANAGER
        return cfg.get("emulator.manager") or DEFAULT_MANAGER

    def _dev_vmindex() -> int:
        try:
            return int(cfg.get("emulator.vmindex") or 0)
        except Exception:
            return 0

    def _handle_scan(cmd):
        if run_in_progress(ROOT):
            return False, (BUSY_PREFIX + "本机正在跑任务，暂不扫描设备（避免打断）"), \
                {"deferred": True}
        from stzb.devices import scan_devices, emulator_status
        devices = scan_devices(_dev_adb(), deep=True, logger=log)
        emu = emulator_status(_dev_manager(), _dev_vmindex(), logger=log)
        # 顺带把「当前配置里写着的候选/本机 client」报回去，方便后端建关联
        payload = {"devices": devices, "emulator": emu,
                   "adb": _dev_adb(),
                   "serial_candidates": list(
                       cfg.get("device.serial_candidates") or []),
                   "package": cfg.get("device.package") or GAME_PKG}
        box.patch_device(emulator_running=bool(emu.get("running")),
                         adb_serial=(devices[0]["serial"] if devices else None))
        online = [d for d in devices if d.get("online")]
        return True, "扫描到 %d 台设备（在线 %d：%s）" % (
            len(devices), len(online),
            "、".join(d["serial"] for d in online) or "无"), payload

    def _handle_start_emulator(cmd):
        from stzb.devices import start_emulator
        if run_in_progress(ROOT):
            return False, (BUSY_PREFIX + "本机正在跑任务，不重复启动模拟器"), \
                {"deferred": True}
        r = start_emulator(_dev_manager(), _dev_vmindex(),
                           package=cfg.get("device.package") or GAME_PKG,
                           adb=_dev_adb(),
                           serial_candidates=cfg.get("device.serial_candidates"),
                           timeout=float(cfg.get("emulator.startup_timeout") or 300),
                           logger=log)
        if r.get("ok"):
            box.patch_device(emulator_running=True,
                             adb_serial=r.get("serial") or None)
            ui_holder["ui"] = None              # 新起来的实例要重新连
            ui_holder["tried"] = 0.0
        return bool(r.get("ok")), r.get("message") or "启动模拟器", r

    def _handle_stop_emulator(cmd):
        from stzb.devices import stop_emulator
        if run_in_progress(ROOT):
            return False, (BUSY_PREFIX + "本机正在跑任务，不能关模拟器"), \
                {"deferred": True}
        r = stop_emulator(_dev_manager(), _dev_vmindex(), logger=log)
        if r.get("ok"):
            box.patch_device(emulator_running=False)
            ui_holder["ui"] = None
        return bool(r.get("ok")), r.get("message") or "关闭模拟器", r

    def _handle_force_size(cmd):
        from stzb.devices import force_landscape
        extra = cmd.get("extra") or {}
        serial = (extra.get("serial") or "").strip()
        if not serial:
            return False, "指令没带 serial（不知道该覆盖哪台设备）", {}
        r = force_landscape(_dev_adb(), serial, logger=log)
        return bool(r.get("ok")), r.get("message") or "分辨率覆盖", r

    def _handle_restore_size(cmd):
        from stzb.devices import restore_size
        extra = cmd.get("extra") or {}
        serial = (extra.get("serial") or "").strip()
        if not serial:
            return False, "指令没带 serial", {}
        r = restore_size(_dev_adb(), serial, logger=log)
        return bool(r.get("ok")), r.get("message") or "恢复分辨率", r

    def _handle_discover_roles(cmd):
        """★ 角色自动发现：连设备 → 读游戏里的角色名 → 上报后端。

        这是「第一次登录后把角色自动保存到后端」的实现，也负责
        「游戏里找不到角色了 → 重新发现并修正后端记录」。

        真正的读取逻辑在 `stzb.account.discover_roles()`：那里默认**只读**
        （绝不点击界面），后端要它点一次「点击换区」时才会通过
        `extra.allow_tap` 传进来 —— 开放点是明确的、由人按下按钮触发的，
        而不是「代理自己看着办」。
        """
        if run_in_progress(ROOT):
            return False, (BUSY_PREFIX + "本机正在跑任务，代理不与它抢游戏界面，"
                                         "已放回队列等本轮结束"), {"deferred": True}
        extra = cmd.get("extra") or {}
        serial = (extra.get("serial") or "").strip()
        allow_tap = bool(extra.get("allow_tap"))
        ui = make_ui(serial) if serial else make_ui()
        if ui is None:
            msg = ("连不上设备（%s）" % serial) if serial else \
                "连不上模拟器/ADB（模拟器可能没开）"
            return False, msg, {}

        from stzb.account import discover_roles
        r = discover_roles(ui, allow_tap=allow_tap, log=log)

        names = r.get("roles") or []
        if names:
            box.patch_current(masked=r.get("masked") or None, role=names[0])
            src = {"role_dialog": "「选择角色」对话框",
                   "home": "主城界面"}.get(r.get("source") or "", "界面")
            msg = "从%s读到 %d 个角色：%s" % (src, len(names), "、".join(names))
        else:
            msg = r.get("note") or "没读到任何角色"

        payload = {
            "roles": names,
            "role": r.get("current") or "",
            "masked": r.get("masked") or "",
            "source": r.get("source") or "",
            "mode": r.get("mode") or "",
            "on_login": bool(r.get("on_login")),
            "on_home": bool(r.get("on_home")),
            "serial": serial or ui.dev.serial,
        }
        try:
            payload["screen"] = describe_screen(ui, limit=12)
        except Exception:
            pass
        return bool(names), msg, payload

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

        # ★ 设备类指令在**连 ui 之前**分派：它们要么是 adb 级操作、
        #   要么是启停进程，本来就不需要 ui；而且「模拟器没开」
        #   正是最该被 start_emulator 救回来的场景，不能先卡在连 ui 上。
        if kind == "adb_scan":
            return _handle_scan(cmd)
        if kind == "start_emulator":
            return _handle_start_emulator(cmd)
        if kind == "stop_emulator":
            return _handle_stop_emulator(cmd)
        if kind == "force_size":
            return _handle_force_size(cmd)
        if kind == "restore_size":
            return _handle_restore_size(cmd)
        if kind == "discover_roles":
            return _handle_discover_roles(cmd)

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
            # ★ 本机有一轮任务在跑时不要抢游戏界面：任务那边以为还在主城，
            #   这里把角色切走 → 后面所有点击全部错位。回报 BUSY 让服务端
            #   把指令放回队列，等本轮结束再执行（管理端那次点击不会白点）。
            if run_in_progress(ROOT):
                return False, (BUSY_PREFIX + "本机正在跑任务，代理不与它抢游戏界面，"
                                             "已放回队列等本轮结束"), {"deferred": True}
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

    # ------------------------------------------------ 后台派任务 → 立刻起一轮
    #
    # 常驻代理只负责「保持连接 + 收到就起」，真正跑任务交给 run_daily：
    # 它自带单实例锁（防两轮同时点模拟器）和完整的报告/上传逻辑，
    # 代理不必（也不该）把这些重复实现一遍。
    #
    # 用 `--job-only` 起，是为了**防退化成常规档位运行**：万一那条任务在我们
    # 决定起进程的这一瞬间被别人抢走或被取消，run_daily 必须直接退出，
    # 而不是顺势把「当前档位的常规任务」跑一遍 —— 那等于凭空多跑一轮，还会重复领奖。
    rt_holder: dict = {"rt": None}
    run_state: dict = {"proc": None}

    def _peek_job_serial() -> str:
        """看一眼队列**第一条**任务要求在哪台设备上跑（没指定就返回空串）。

        为什么要先看：任务可以指定设备（后端「设备管理」页/待执行任务页选的）。
        run_daily 自己也会领任务并读出设备，但那要等它启动、连上后端之后才发生；
        这里先看一眼，能把目标设备当成 `--serial` 直接传下去 —— run_daily 一启动
        就知道自己该不该碰模拟器（`manage_emu` 判据依赖它），少一次来回。

        ★ 只认「本机有权领的那条」（list_jobs 已经按客户端过滤过了）。
          第一条**没指定设备**时返回空串 —— 那表示「按本机配置自己选」，
          不能顺手拿后面某条的设备去当目标，那会让通用任务被顶到别的设备上跑。
        """
        # 环境变量已经把本机钉在某一台设备上时，不覆盖用户的意图
        if str(os.environ.get("STZB_DEVICE_SERIAL") or "").strip():
            return ""
        try:
            ok, data = client.list_jobs()
        except Exception:
            return ""
        jobs = ((data or {}).get("jobs") or []) if ok else []
        if not jobs:
            return ""
        return str(jobs[0].get("serial") or "").strip()

    def _spawn_run(why: str) -> bool:
        proc = run_state.get("proc")
        if proc is not None and proc.poll() is None:
            log("  · 上一轮还在跑（pid=%s），这次不重开" % proc.pid)
            return False
        if run_in_progress(ROOT):
            # 可能是定时任务起的，也可能是别人手动跑的 —— 反正现在不能抢
            log("  · 本机已有一轮任务在跑，%s（任务留在队列里）" % why)
            return False
        cmd = [sys.executable, os.path.join(ROOT, "run_daily.py"), "--job-only"]
        dev_serial = _peek_job_serial()
        if dev_serial:
            cmd += ["--serial", dev_serial]
        log_file = os.path.join(LOG_DIR, "agent_run.log")
        try:
            fh = io.open(log_file, "a", encoding="utf-8")
            fh.write("\n===== %s 常驻代理起一轮：%s =====\n"
                     % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), why))
            fh.write("     目标设备：%s\n" % (dev_serial or "（未指定，按本机配置）"))
            fh.flush()
            proc = subprocess.Popen(
                cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
        except Exception as e:
            log("  ! 起 run_daily 失败：%r" % (e,))
            return False
        run_state["proc"] = proc
        box.set(state="starting", busy=True, note="正在按后台任务跑一轮")
        log("  ✓ 已起一轮 run_daily（pid=%s，%s%s），输出见 logs/agent_run.log"
            % (proc.pid, why,
               ("，设备=" + dev_serial) if dev_serial else ""))
        threading.Thread(target=_wait_run, args=(proc,), name="stzb-run-wait",
                         daemon=True).start()
        return True

    def _wait_run(proc) -> None:
        """等这一轮跑完，再把队列里**剩下**的任务接着跑掉。

        为什么结束时还要看一眼队列：后台可能连着派了好几条，而每条只推一次通知，
        第二条到达时代理正忙着（上面会拒绝重开）。不补这一眼的话，第二条就得等到
        下一档 —— 而「随时指定任务」正是这次改动的目的，等到下一档等于没做。

        这一眼也顺带兜住了「实时通道抖动导致 notify 丢失」：只要有一次状态同步
        成功，`pending_jobs` 就会告诉我们队列里还有东西（见 Realtime._apply）。
        """
        try:
            code = proc.wait()
        except Exception:
            code = -1
        run_state["proc"] = None
        log("  · 这一轮 run_daily 结束（退出码 %s）" % code)
        box.set(state="idle", busy=False, note="常驻代理在线（未在跑任务）")
        try:
            ok, data = client.list_jobs()
            jobs = ((data or {}).get("jobs") or []) if ok else []
        except Exception:
            jobs = []
        if jobs:
            log("  · 队列里还有 %d 条任务，接着跑" % len(jobs))
            _spawn_run("队列里还有 %d 条任务" % len(jobs))
        elif rt_holder["rt"] is not None:
            rt_holder["rt"].beat_now()     # 让后台立刻看到「空闲了」

    def on_notify(what, msg):
        if what == "jobs":
            if args.no_run:
                log("  · 后台派了任务；本代理以 --no-run 启动，不自动起一轮（留给下一档）")
                return
            rt = rt_holder["rt"]
            if rt is not None and rt.task_paused:
                log("  · 后台派了任务，但这台客户端被设为「暂停派发」→ 不跑")
                return
            _spawn_run("后台派了任务%s"
                       % ((" #%s" % msg.get("job")) if msg.get("job") else ""))
        elif what == "config":
            log("  · 后台配置已更新到 v%s（本轮不重拉，下次 run_daily 生效）"
                % msg.get("version", "?"))

    rt = Realtime(client, identity, state_fn=box.get,
                  on_assignment=on_assignment, on_command=on_command,
                  on_notify=on_notify,
                  logger=log,
                  interval=args.interval or int((info or {}).get("heartbeat_interval") or 30))
    rt_holder["rt"] = rt
    rt.start()
    log("代理已进入常驻状态。按 Ctrl+C 退出。")
    if args.no_probe:
        log("  注意：以 --no-probe 运行，不会响应「探测」「强制切换」指令。")
    if args.no_run:
        log("  注意：以 --no-run 运行，后台派的任务不会自动起一轮。")

    # 把连上的传输层说清楚（实时通道由后台线程去连，给它一点时间）
    for _ in range(30):
        if rt.ws_connected or (rt.transport == "http" and rt.beats):
            break
        time.sleep(0.2)
    if rt.ws_connected:
        log("  传输方式   : 实时长连接（%s）—— 后台的指令/任务会即时送达" % rt.url)
    else:
        log("  传输方式   : 心跳兜底（每 %s 秒一次），实时通道会自动重试。原因：%s"
            % (rt.interval, rt.ws_error or "尚未连上"))

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，代理退出。")
    finally:
        if run_state.get("proc") is not None and run_state["proc"].poll() is None:
            log("  （本机还有一轮 run_daily 在跑，它是独立进程，代理退出不影响它）")
        rt.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())