# -*- coding: utf-8 -*-
"""率土之滨每日任务 —— 入口。

全流程（默认就是你想要的那一串，不需要额外参数）：
    拉起模拟器 → 等 Android 启动完 → 连 ADB → 打开游戏 → 进到主城 → 跑任务 → 出报告

用法：
    python run_daily.py --slot 00:00        # 跑 00:00 档
    python run_daily.py --slot 12:00        # 跑 12:00 档（多一个「特性」）
    python run_daily.py --slot auto         # 按当前时间自动选档，并跳过今天已跑过的档
    python run_daily.py --only recruit      # 只跑某个任务
    python run_daily.py --only recruit --dry-run   # 只识别不点击（安全预演）
    python run_daily.py --list              # 看看有哪些任务
    python run_daily.py --status            # 只看模拟器/ADB/游戏状态，什么都不做
    python run_daily.py --open-report       # 跑完自动用默认浏览器打开报告

模拟器相关开关：
    --cold          跑之前先把 MuMu 完全关掉，做一次干净冷启动
    --shutdown-after 跑完把模拟器关掉
    --keep-running  跑完保持模拟器开着（覆盖配置）
    --no-emulator   完全不动模拟器（自己手动开好了再用这个）

任务 key：gongpin / shuishou / shijing / yanwu / texing / recruit
配置在 config.json（半价招募开关、模拟器设置都在里面）
执行报告在 logs/reports/，最新的两份固定叫 latest.html / latest.json
"""
from __future__ import annotations

import argparse
import atexit
import datetime as dt
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from stzb.cloud import upload_run, load_last_report, CloudClient, CloudError, hostname
from stzb.config import load                      # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device   # noqa: E402
from stzb.emulator import MuMu                    # noqa: E402
from stzb.remote_config import apply_remote, pick_job   # noqa: E402
from stzb.report import RunReport                 # noqa: E402
from stzb.tasks import TASKS, run_all             # noqa: E402
from stzb.ui import Ui                            # noqa: E402

STATE = os.path.join(ROOT, "state", "last_run.json")
REMOTE_STATE = os.path.join(ROOT, "state", "remote_config.json")   # 远端配置落地记录
LOCK = os.path.join(ROOT, "state", "run.lock")
LOG_DIR = os.path.join(ROOT, "logs")
SHOT_DIR = os.path.join(ROOT, "logs", "shots")
REPORT_DIR = os.path.join(ROOT, "logs", "reports")

# 单实例锁的最长有效期。正常一轮 10 分钟以内（含冷启动模拟器），超过就当成上次异常退出留下的。
LOCK_STALE_SECONDS = 2400


def pid_alive(pid: int) -> bool:
    """判断进程是否还活着（跨平台，不抛异常）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259      # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


class RunLock:
    """同一台模拟器同一时刻只允许一个实例操控。

    没有这把锁的时候踩过坑：00:00 的定时任务还没跑完，手动又起了一轮，
    两个进程同时 ADB 点击，日志互相交错、点击全部错位，还会误点到花钱的按钮。
    """

    def __init__(self, path, logger):
        self.path = path
        self.log = logger
        self.held = False

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        info = None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception:
            info = None

        if info:
            pid = int(info.get("pid") or 0)
            started = info.get("started") or ""
            try:
                age = time.time() - dt.datetime.fromisoformat(started).timestamp()
            except Exception:
                age = LOCK_STALE_SECONDS + 1
            if age < LOCK_STALE_SECONDS and pid_alive(pid):
                self.log("!! 已有一个实例在运行（pid=%s，开始于 %s，已 %.0f 秒）"
                         % (pid, started, age))
                self.log("!! 两个进程同时点模拟器会互相打架，本次直接退出。")
                return False
            self.log("  · 发现残留锁（pid=%s 已不在或已过期），接管。" % pid)

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(),
                       "started": dt.datetime.now().isoformat(timespec="seconds")},
                      f, ensure_ascii=False)
        self.held = True
        atexit.register(self.release)
        return True

    def release(self):
        if not self.held:
            return
        self.held = False
        try:
            os.remove(self.path)
        except Exception:
            pass


def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


class Logger:
    def __init__(self, path):
        self.f = io.open(path, "a", encoding="utf-8")

    def __call__(self, msg):
        line = "[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg)
        try:
            print(line, flush=True)
        except Exception:
            pass
        self.f.write(line + "\n")
        self.f.flush()


class Tee:
    """同时往多个 logger 写。给启动阶段用：既进日志，也进报告的「启动过程」。"""

    def __init__(self, *sinks):
        self.sinks = [s for s in sinks if s]

    def __call__(self, msg):
        for s in self.sinks:
            try:
                s(msg)
            except Exception:
                pass


def make_emulator(cfg, log) -> MuMu:
    e = cfg.get("emulator", {}) or {}
    return MuMu(manager=e.get("manager"),
                vmindex=e.get("vmindex", 0),
                adb=cfg.get("device.adb") or DEFAULT_ADB,
                serial_candidates=cfg.get("device.serial_candidates"),
                logger=log,
                startup_timeout=e.get("startup_timeout", 300))


def wait_game_ready(dev: Device, pkg: str, timeout: float = 90.0,
                    log=print) -> bool:
    """等游戏进程真的起来（不是图标点下去就算）。"""
    end = time.time() + timeout
    t0 = time.time()
    while time.time() < end:
        if dev.game_running(pkg):
            log("  ✓ 游戏进程已启动（%.0f 秒）" % (time.time() - t0))
            return True
        time.sleep(2)
    log("  × 等了 %.0f 秒游戏进程仍没起来" % timeout)
    return False


def cmd_status(cfg, offline: bool = False):
    """只看状态，不动任何东西。"""
    e = cfg.get("emulator", {}) or {}
    emu = MuMu(manager=e.get("manager"), vmindex=e.get("vmindex", 0),
               adb=cfg.get("device.adb") or DEFAULT_ADB,
               serial_candidates=cfg.get("device.serial_candidates"),
               logger=lambda m: None)
    print(emu.status_line())
    online = emu.adb_online()
    print("ADB 在线设备：%s" % (online or "无"))
    if online:
        dev = Device(adb=cfg.get("device.adb") or DEFAULT_ADB,
                     serial_candidates=cfg.get("device.serial_candidates"),
                     shot_dir=SHOT_DIR)
        if dev.connect():
            fg = dev.foreground()
            print("前台窗口：%s" % (fg or "读不到"))
            print("游戏在跑：%s" % dev.game_running(cfg.get("device.package") or GAME_PKG))
    st = load_state()
    print("上次跑完：%s" % (st.get("last_finish") or "无记录"))
    print("今天已跑档位：%s" % (", ".join(st.get("slots", []))
                            if st.get("date") == dt.date.today().isoformat() else "无"))

    # ---- 运行模式：这一条最容易被搞混，必须写清楚 ----
    cloud = cfg.get("cloud") or {}
    mode = resolve_mode(cfg, offline=offline, log=lambda m: None)
    print("")
    print("运行模式：%s" % describe_mode(cfg, mode))
    if mode == MODE_STANDALONE:
        if offline:
            print("           （命令行给了 --offline；就算配了后端也不连）")
        elif not cloud.get("enabled"):
            print("           （config.json 里 cloud.enabled = false；要绑定后端就改成 true）")
        else:
            print("           （cloud.enabled 是 true，但 base_url / token 没填全）")
        print("           本轮不会连任何外部服务，报告只留在本机 logs\\reports\\")
    else:
        token = str(cloud.get("token") or "")
        shown = ("%s…%s" % (token[:8], token[-4:])) if len(token) > 12 else "已配置"
        print("           令牌：%s" % shown)
        try:
            client = CloudClient(base_url=cloud.get("base_url", ""), token=token,
                                 timeout=8, retries=1, logger=lambda m: None)
            ok, info = client.ping()
            if ok:
                print("           探活：✓ 连得上（远端配置版本 v%s）"
                      % (info or {}).get("config_version", "?"))
            else:
                print("           探活：× 连不上 —— %s" % info)
                print("           （连不上也不影响跑任务，报告仍会留在本机）")
        except Exception as ex:
            print("           探活：× %r" % (ex,))
    return 0


def _apply_cloud_env(cfg) -> None:
    """用环境变量覆盖后端设置，方便测试、也方便把令牌放到系统里而不写进 config.json。"""
    cloud = cfg.setdefault("cloud", {})
    for env, key, cast in (("STZB_CLOUD_BASE_URL", "base_url", str),
                           ("STZB_CLOUD_TOKEN", "token", str),
                           ("STZB_CLOUD_ENABLED", "enabled",
                            lambda v: str(v).strip().lower() in ("1", "true", "yes", "on"))):
        v = os.environ.get(env)
        if v not in (None, ""):
            cloud[key] = cast(v)


MODE_STANDALONE = "standalone"
MODE_MANAGED = "managed"


def resolve_mode(cfg, offline: bool, log) -> str:
    """决定这一轮是「独立运行」还是「后端托管」。

    **独立运行是默认且安全的那一端**：不拉远端配置、不领待执行任务、不上传任何东西，
    除了游戏和模拟器之外不碰任何外部服务。整套脚本拷到另一台机器、不配任何后端也能直接跑。

    判定顺序：
      1. 命令行给了 --offline        → 独立运行（优先级最高，用来临时脱离后端）
      2. cloud.enabled 不是 true     → 独立运行（默认值就是 false）
      3. 开了但 base_url/token 没填全 → 降级成独立运行，并明确告警
      4. 其余                        → 后端托管
    """
    cloud = cfg.get("cloud") or {}
    if offline:
        return MODE_STANDALONE
    if not cloud.get("enabled"):
        return MODE_STANDALONE
    if not str(cloud.get("base_url") or "").strip() or not str(cloud.get("token") or "").strip():
        log("  ! 后端配置不完整（缺 base_url 或 token）→ 本轮按独立运行处理")
        return MODE_STANDALONE
    return MODE_MANAGED


def describe_mode(cfg, mode: str) -> str:
    if mode == MODE_STANDALONE:
        return "独立运行（不连后端，报告只留在本机）"
    return "后端托管 %s（拉配置 / 领任务 / 上传结果）" % (
        (cfg.get("cloud") or {}).get("base_url") or "")


def _cloud_ready(cfg, log) -> bool:
    """给 --upload-last 这类「只有连后端才有意义」的命令用。"""
    cloud = (cfg.get("cloud") or {})
    if not cloud.get("enabled"):
        return False
    if not cloud.get("token"):
        log("  ! cloud.enabled=true 但没填 token，跳过上传（本机报告不受影响）")
        return False
    return True


def cmd_upload_last(cfg, offline: bool = False) -> int:
    """只补传本地最近一份报告，不跑任务。网络恢复后用它兜底。"""
    print("· 补传本地最近一份报告…")
    if offline:
        print("!! 指定了 --offline（独立运行模式），不会连接后端。")
        print("   想补传就别加 --offline。")
        return 1
    got = load_last_report(REPORT_DIR)
    if not got:
        print("!! logs/reports/latest.json 不存在，没有可补传的东西")
        return 1
    stub, paths = got
    print("· 本地报告：%s" % (paths.get("html") or "（没有 html）"))
    print("· 轮次：%s 档位 %s" % (stub._data.get("started_at"), stub.slot))
    if not _cloud_ready(cfg, print):
        print("!! 后端未启用。请在 config.json 的 cloud 段填好 base_url 与 token，")
        print("   或设环境变量 STZB_CLOUD_ENABLED=1 / STZB_CLOUD_BASE_URL / STZB_CLOUD_TOKEN。")
        return 1
    res = upload_run(cfg, stub, paths, logger=print,
                     shots_per_task=int((cfg.get("cloud") or {}).get("upload_shots_per_task", 4)))
    if not res.get("ok"):
        print("!! 补传失败：%s" % res.get("error"))
        return 1
    print("✓ 补传完成（运行 #%s，报告 %s，截图 %d 张）"
          % (res.get("run_id"), "已传" if res.get("report_ok") else "未传",
             res.get("shots_ok") or 0))
    return 0


def _do_upload(cfg, report, paths, client, job, log, disabled=False, reason=""):
    """上传结果 + 给待执行任务回执。**任何失败都不抛异常**，也不影响本机报告。

    这是刻意的：上报告是尽力而为。网络断了、服务器挂了，已经跑完的任务不能白跑 ——
    本机 logs/reports/ 里那份永远完整，网络恢复后用 --upload-last 补传即可。

    独立运行模式下这里直接返回，**一个字节都不往外发**。
    """
    if disabled or client is None:
        if disabled:
            log("· %s，报告只留在本机：%s"
                % (reason or "跳过上传", paths.get("html") or ""))
        return None
    log("· 上传到后端控制台…")
    try:
        res = upload_run(cfg, report, paths, logger=log,
                         shots_per_task=int((cfg.get("cloud") or {})
                                            .get("upload_shots_per_task", 4)))
    except Exception as e:                      # 上传模块自身的 bug 也不能拖垮收尾
        log("  × 上传过程异常：%r（本机报告不受影响）" % (e,))
        res = {"ok": False, "error": repr(e), "run_id": None}

    if job:
        try:
            client.ack_job(int(job["id"]), "done" if res.get("ok") else "failed",
                           res.get("run_id"))
            log("  · 待执行任务 #%s 已回执：%s"
                % (job["id"], "完成" if res.get("ok") else "失败"))
        except Exception as e:
            log("  ! 回执失败：%r" % (e,))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="auto", choices=["auto", "00:00", "12:00"])
    ap.add_argument("--only", default=None, help="逗号分隔的任务 key，或用 all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略今天已跑过的记录")
    ap.add_argument("--list", action="store_true", help="列出所有任务")
    ap.add_argument("--status", action="store_true", help="只看模拟器/游戏状态")
    ap.add_argument("--cold", action="store_true", help="跑之前先完全关掉 MuMu，冷启动")
    ap.add_argument("--shutdown-after", action="store_true", help="跑完关掉模拟器")
    ap.add_argument("--keep-running", action="store_true", help="跑完保持模拟器开着")
    ap.add_argument("--no-emulator", action="store_true", help="不动模拟器（自己已开好）")
    ap.add_argument("--open-report", action="store_true", help="跑完自动打开报告")
    ap.add_argument("--no-upload", action="store_true", help="本轮不上传到后端")
    ap.add_argument("--no-remote-config", action="store_true", help="不从后端拉配置，只用本地")
    ap.add_argument("--offline", "--standalone", dest="offline", action="store_true",
                    help="独立运行：完全不连后端（不拉配置、不领任务、不上传），"
                         "优先级高于 config.json")
    ap.add_argument("--upload-last", action="store_true",
                    help="不跑任务，只把本地最近的报告补传到后端")
    args = ap.parse_args()

    if args.list:
        for k, m in TASKS.items():
            print("  %-10s %-28s 档位=%s" % (k, m["name"], ",".join(m["at"])))
        return 0

    os.makedirs(SHOT_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)
    cfg = load()
    _apply_cloud_env(cfg)          # 允许用环境变量覆盖后端地址/令牌（敏感值不必落盘）

    if args.status:
        return cmd_status(cfg, offline=args.offline)

    if args.upload_last:
        return cmd_upload_last(cfg, offline=args.offline)

    log = Logger(os.path.join(LOG_DIR, "run_%s.log" % dt.date.today().isoformat()))
    today = dt.date.today().isoformat()
    now = dt.datetime.now()
    slot = args.slot
    if slot == "auto":
        slot = "12:00" if now.hour >= 12 else "00:00"

    only = None
    if args.only:
        if args.only == "all":
            only = list(TASKS.keys())          # 无视档位，全跑一遍
        else:
            only = [x.strip() for x in args.only.split(",") if x.strip()]
    dry_run = args.dry_run
    force = args.force

    # ------------------------------------------------ 运行模式 + 后端：拉配置 / 领任务
    # 必须放在「今天这一档跑过没」的判断**之前**：后端排的请求优先级最高，
    # 哪怕今天已经跑过，只要有人在控制台点了一次「执行」，就要照跑。
    mode = resolve_mode(cfg, args.offline, log)
    managed = (mode == MODE_MANAGED)
    log("· 运行模式：%s" % describe_mode(cfg, mode))
    # 跳过上传时要说清是「模式决定」还是「命令行显式要求」，否则日志里看不出区别
    upload_skip_reason = "独立运行模式（不连后端）" if not managed else "按 --no-upload 跳过上传"

    client = None
    job = None
    if managed:
        try:
            client = CloudClient(base_url=cfg["cloud"]["base_url"],
                                 token=cfg["cloud"]["token"],
                                 timeout=int(cfg["cloud"].get("timeout", 90)),
                                 retries=int(cfg["cloud"].get("retries", 3)),
                                 logger=log)
        except CloudError as e:
            log("  ! 后端客户端初始化失败：%s" % e)

    if client is not None:
        ok, info = client.ping()
        if ok:
            log("· 后端已连接：%s（远端配置版本 v%s）"
                % (cfg["cloud"]["base_url"], (info or {}).get("config_version", "?")))
        else:
            log("  ! 后端探活失败：%s（继续跑，跑完再试上传）" % info)

        if cfg["cloud"].get("pull_config", True) and not args.no_remote_config:
            apply_remote(cfg, client, REMOTE_STATE, logger=log)
        elif args.no_remote_config:
            log("· 按 --no-remote-config 跳过远端配置")

        if cfg["cloud"].get("pull_jobs", True):
            job = pick_job(client, logger=log)
        if job:
            if job.get("slot") and job["slot"] != "auto":
                slot = job["slot"]
            jonly = (job.get("only") or "").strip()
            if jonly:
                only = list(TASKS.keys()) if jonly == "all" \
                    else [x.strip() for x in jonly.split(",") if x.strip()]
            if job.get("dry_run"):
                dry_run = True
            force = True
            log("· 本轮由控制台的待执行任务驱动：档位=%s 范围=%s 预演=%s"
                % (slot, jonly or "按档位全部", dry_run))
            # 配置是刚拉下来的，任务范围要按新配置再核一遍
            if only:
                enabled = cfg.get("tasks", {}) or {}
                skipped = [k for k in only if not enabled.get(k, True)]
                if skipped:
                    log("  · 以下任务在远端配置里被关闭，本轮跳过：%s" % ",".join(skipped))
                    only = [k for k in only if enabled.get(k, True)] or None

    st = load_state()
    if not force and not only:
        if st.get("date") == today and slot in st.get("slots", []):
            log("今天 %s 档已经跑过了，跳过。（要强制重跑加 --force）" % slot)
            return 0

    # 说明文字放在这里算：任务队列可能覆盖过 only / slot，要按最终结果写
    only_label = ("、".join(only) if only else "%s 档全部" % slot)

    log("=" * 70)
    log("启动：档位=%s dry_run=%s only=%s" % (slot, dry_run, only_label))

    lock = RunLock(LOCK, log)
    if not lock.acquire():
        if client is not None and job:
            client.ack_job(int(job["id"]), "failed", None)
        return 2

    # ---------------------------------------------------------------- 报告 + 环境
    report = RunReport(ROOT, SHOT_DIR, slot=slot, dry_run=dry_run, logger=log)
    rlog = Tee(log, report.note)          # 启动阶段的日志同时进报告
    emu_cfg = cfg.get("emulator", {}) or {}
    pkg = cfg.get("device.package") or GAME_PKG
    emu = make_emulator(cfg, rlog)

    report.env_info(模拟器="MuMu 12（vmindex=%s）" % emu_cfg.get("vmindex", 0),
                    启动时间=report.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                    运行模式=describe_mode(cfg, mode),
                    档位=slot,
                    模式="预演（不点击）" if dry_run else "实际执行",
                    任务范围=only_label),

    emu_started_by_us = False
    shutdown_after = (args.shutdown_after
                      or (emu_cfg.get("shutdown_after") == "always" and not args.keep_running))
    auto_shutdown_auto = (emu_cfg.get("shutdown_after", "auto") == "auto"
                          and not args.keep_running)
    t_run0 = time.time()

    def _bail(code: int, reason: str) -> int:
        """启动阶段失败的统一出口：落报告 → 上传 → 回执 → 返回。

        启动失败同样值得上传：报告里会带上「启动过程」的日志和当时的截图，
        这正是排查「为什么没跑起来」最需要的东西。
        """
        rlog("!! %s，退出。" % reason)
        report.env_info(结果=reason)
        p = report.write_all(REPORT_DIR, cfg.get("logging.keep_days", 14))
        rlog("· 报告：%s" % p["html"])
        _do_upload(cfg, report, p, client, job, rlog, disabled=(args.no_upload or not managed), reason=upload_skip_reason)
        return code

    # ---------------------------------------------------------------- 1. 模拟器
    if args.no_emulator:
        rlog("· 按 --no-emulator 跳过模拟器管理，直接连 ADB")
    else:
        rlog("· 检查模拟器…")
        if args.cold or emu_cfg.get("cold_restart"):
            rlog("  · 冷启动：先把 MuMu 完全关掉")
            emu.stop(wait=True, timeout=90)      # 先正常关机
            emu.kill_all()                       # 再把主程序窗口也收掉
        if emu.is_running():
            rlog("  · 模拟器已在运行，等它就绪")
            if not emu.wait_ready():
                return _bail(3, "模拟器不就绪")
        else:
            emu_started_by_us = True
            t0 = time.time()
            if not emu.start(package=pkg, wait=True):
                return _bail(3, "模拟器启动失败")
            report.env_info(模拟器启动耗时="%.0f 秒" % (time.time() - t0))

    # ---------------------------------------------------------------- 2. ADB
    dev = Device(adb=cfg.get("device.adb") or DEFAULT_ADB,
                 serial_candidates=cfg.get("device.serial_candidates"),
                 shot_dir=SHOT_DIR)
    if not dev.connect(retries=6, wait=2.0):
        return _bail(3, "ADB 连接失败")
    rlog("· ADB 已连接：%s" % dev.serial)
    report.env_info(ADB设备=dev.serial)

    # ---------------------------------------------------------------- 3. 游戏
    if dev.game_running(pkg):
        rlog("· 游戏已经在运行，直接进主城")
        report.env_info(游戏启动="本来就在跑")
    else:
        t0 = time.time()
        rlog("· 打开游戏 %s" % pkg)
        dev.launch(pkg)
        if not wait_game_ready(dev, pkg, timeout=120, log=rlog):
            return _bail(4, "游戏启动失败")
        report.env_info(游戏启动耗时="%.0f 秒" % (time.time() - t0))

    ui = Ui(dev, cfg, logger=rlog, dry_run=dry_run)

    t0 = time.time()
    if not ui.boot():
        return _bail(4, "进主城失败")
    report.env_info(进入主城耗时="%.0f 秒" % (time.time() - t0))

    # ---------------------------------------------------------------- 4. 任务
    started = time.time()
    res = run_all(ui, cfg, slot, only=only, logger=log, report=report)

    log("-" * 70)
    for k, v in res.items():
        log("  %-10s %s" % (k, "OK" if v else "失败/跳过"))
    log("耗时 %.1f 秒；截图存于 %s" % (time.time() - started, SHOT_DIR))

    # ---------------------------------------------------------------- 5. 报告
    report.env_info(总耗时="%.1f 秒" % (time.time() - t_run0))
    paths = report.write_all(REPORT_DIR, cfg.get("logging.keep_days", 14))
    log("=" * 70)
    log("执行结果：")
    for line in report.summary_text().splitlines():
        log("  " + line)
    log("报告（含截图）：%s" % paths["html"])
    log("结构化结果：%s" % paths["latest_json"])

    # ---------------------------------------------------------------- 6. 收尾
    if not args.no_emulator:
        if shutdown_after or (auto_shutdown_auto and emu_started_by_us):
            why = "--shutdown-after / 配置 shutdown_after=always" if shutdown_after \
                else "本次是脚本自己启动的（shutdown_after=auto）"
            log("· 收尾：关闭模拟器（%s）" % why)
            try:
                dev.force_stop(pkg)
                time.sleep(1)
            except Exception:
                pass
            emu.stop()
        else:
            log("· 收尾：保持模拟器运行（下次跑会复用）")

    # ---------------------------------------------------------------- 7. 上传后端
    # 放在关模拟器之后：先把占内存的虚拟机放掉，再慢慢传。
    _do_upload(cfg, report, paths, client, job, log, disabled=(args.no_upload or not managed), reason=upload_skip_reason)

    if not only:
        if st.get("date") != today:
            st = {"date": today, "slots": []}
        if slot not in st["slots"]:
            st["slots"].append(slot)
        st["last_finish"] = dt.datetime.now().isoformat(timespec="seconds")
        st["last_report"] = paths["html"]
        save_state(st)

    if args.open_report:
        try:
            os.startfile(paths["html"])          # noqa: S606  (Windows 专用)
        except Exception as e:
            log("  （自动打开报告失败：%r）" % e)

    return 0 if all(res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
