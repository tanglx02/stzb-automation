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
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from stzb.account import ensure_target, describe_screen
from stzb.cleanup import cleanup_by_days, parse_cfg as parse_cleanup_cfg, summary_line
from stzb.cloud import upload_run, load_last_report, CloudClient, CloudError, hostname
from stzb.config import load                      # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device   # noqa: E402
from stzb.emulator import MuMu                    # noqa: E402
from stzb.heartbeat import StatusBox             # noqa: E402
from stzb.realtime import BUSY_PREFIX, Realtime   # noqa: E402
from stzb.identity import (Identity, LOCK_STALE_SECONDS, pid_alive,  # noqa: E402
                           read_assignment_cache, run_lock_path,
                           write_assignment_cache)
from stzb.remote_config import apply_remote, pick_job   # noqa: E402
from stzb.report import RunReport                 # noqa: E402
from stzb import task_plan as tplan               # noqa: E402
from stzb.tasks import TASKS, run_all             # noqa: E402
from stzb.ui import Ui                            # noqa: E402

STATE = os.path.join(ROOT, "state", "last_run.json")
REMOTE_STATE = os.path.join(ROOT, "state", "remote_config.json")   # 远端配置落地记录
CLIENT_STATE = os.path.join(ROOT, "state", "client.json")          # 客户端稳定标识
ASSIGN_CACHE = os.path.join(ROOT, "state", "assignment.json")      # 后端指派缓存
# 单实例锁：位置与「进程还活着吗」的判断统一放在 stzb.identity ——
# 常驻代理 agent.py 要用同一套口径判断「现在能不能接后台派的活」。
LOCK = run_lock_path(ROOT)
LOG_DIR = os.path.join(ROOT, "logs")
SHOT_DIR = os.path.join(ROOT, "logs", "shots")
REPORT_DIR = os.path.join(ROOT, "logs", "reports")

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
        # 这是 atexit 收尾，**绝不能往外抛**：抛了会在退出时打一堆栈、
        # 还会污染退出码。删不掉也无所谓 —— acquire() 认得出「pid 已不在」
        # 的残留锁会自己接管，所以残留锁不会把下次运行拦死。
        try:
            os.remove(self.path)
        except BaseException:            # noqa: BLE001 收尾不容中断
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


def cmd_status(cfg):
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

    # ---- 后端绑定状态：这一条最容易被搞混，必须写清楚 ----
    cloud = cfg.get("cloud") or {}
    mode = resolve_mode(cfg, offline=False, log=lambda m: None)
    print("")
    print("后端模式：%s" % describe_mode(cfg, mode))
    if mode == MODE_UNBOUND:
        if not cloud.get("base_url"):
            print("           （缺 base_url —— 还没在 config_tool.bat 的「后端托管」里填地址）")
        else:
            print("           （缺 token —— 后端「设置」页可以重新生成，再填回本机）")
        print("           本轮不会连任何外部服务，报告只留在本机 logs\\reports\\")
        print("           注意：这样后台控制台看不到这台机器，也没法远程派活。")
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
    """用环境变量覆盖后端设置，方便测试、也方便把令牌放到系统里而不写进 config.json。

    ★ `STZB_CLOUD_ENABLED` 已随「独立模式」一起废弃（2026-09-20）：
      现在只看 base_url + token 齐不齐，`enabled` 这个开关不再参与任何判定。
      旧环境变量留着不报错，只是被忽略 —— 免得写死的部署脚本一夜之间全挂。
    """
    cloud = cfg.setdefault("cloud", {})
    for env, key, cast in (("STZB_CLOUD_BASE_URL", "base_url", str),
                           ("STZB_CLOUD_TOKEN", "token", str)):
        v = os.environ.get(env)
        if v not in (None, ""):
            cloud[key] = cast(v)


MODE_MANAGED = "managed"
MODE_UNBOUND = "unbound"        # 没配后端地址/令牌 —— 不是「模式」，是「还没绑上」


def resolve_mode(cfg, offline: bool, log) -> str:
    """本脚本**只有一种模式：后端托管**（2026-09-20 起，独立模式已按用户要求移除）。

    所以这里不再返回「独立/托管」两种模式，而是返回一个**连接状态**：

      · `MODE_MANAGED` —— 后端地址与令牌都配齐了，正常托管；
      · `MODE_UNBOUND` —— 缺 base_url 或 token（还没在配置工具里绑后端）。
        这**不是**一种运行模式，而是「还没绑上」的中间状态：
        脚本照常跑任务、报告照常留在本机，只是拉不到远端配置、领不到待执行任务、
        也上传不了。日志里会明确告警，而不是像以前那样静默当成「独立模式」。

    ★ 为什么要保留 `MODE_UNBOUND` 而不是直接报错退出：
      后端临时连不上（断电、重启、网线掉了）时，**已经到点的日常任务不能白跑**。
      本机报告始终完整，网络恢复后用 `--upload-last` 补传。这是刻意的取舍。

    `offline` 参数已废弃（保留签名只为不破坏老调用），传什么都当 False。
    """
    cloud = cfg.get("cloud") or {}
    url = str(cloud.get("base_url") or "").strip()
    tok = str(cloud.get("token") or "").strip()
    if not url or not tok:
        log("  ! 还没绑定后端（缺 base_url 或 token）—— 本轮照常跑，"
            "但拉不到配置、领不到任务、也上传不了")
        log("    绑后端：双击 config_tool.bat → 「后端托管」；或设环境变量 "
            "STZB_CLOUD_BASE_URL / STZB_CLOUD_TOKEN")
        return MODE_UNBOUND
    return MODE_MANAGED


def describe_mode(cfg, mode: str) -> str:
    url = (cfg.get("cloud") or {}).get("base_url") or ""
    if mode == MODE_UNBOUND:
        return "未绑定后端（照常跑任务，报告只留在本机）"
    return "后端托管 %s（拉配置 / 领任务 / 上传结果）" % url


def _cloud_ready(cfg, log) -> bool:
    """给 --upload-last 这类「只有连后端才有意义」的命令用。"""
    cloud = (cfg.get("cloud") or {})
    if not str(cloud.get("base_url") or "").strip():
        log("  ! 还没绑定后端（缺 base_url），跳过上传（本机报告不受影响）")
        return False
    if not cloud.get("token"):
        log("  ! 还没绑定后端（缺 token），跳过上传（本机报告不受影响）")
        return False
    return True


# ---------------------------------------------------------------- 心跳 / 在线探测

class RealtimeSession:
    """后台实时会话的一条龙封装（长连接优先，心跳降级兜底）。

    服务端在别处、客户端在内网，服务端连不上客户端 —— 所以「后台看到客户端在线」
    全靠这个线程主动上报。它同时承担两个职责：
      1. 让后台知道「我在线、我在干什么、我现在是哪个账号」
      2. 把后台点/派下来的「探测」「强制切换」即时接住

    ★ 传输层由 `stzb.realtime.Realtime` 决定：**WebSocket 长连接优先**，
      连不上（防火墙 / 反代没配 Upgrade / 网络抖动）就自动退回原来的 HTTP 心跳，
      并周期性重试长连接。两种模式下本类的能力完全一样，只是快慢不同。

    刻意做成「用完就丢」的上下文管理器：主流程跑任务期间它在后台转，
    任务一结束就停掉 —— 跑完就退出的进程没必要挂着连接
    （要「后台随时能戳到」请用常驻的 agent.py）。
    """

    def __init__(self, client, identity, mode: str, logger):
        self.client = client
        self.identity = identity
        self.log = logger
        self.box = StatusBox(mode=mode)
        self.rt: Optional[Realtime] = None
        self.assignment: Dict[str, Any] = {}
        self._ui = None            # 需要时才建（探测指令要用它截屏 OCR）
        self._make_ui = None
        self.jobs_seen = 0         # 运行期间后台又派了几次任务

    def bind_ui_factory(self, fn) -> None:
        """挂一个「怎么造 Ui 对象」的工厂。收到探测指令时才用它。"""
        self._make_ui = fn

    def start(self, interval: Optional[int] = None) -> "RealtimeSession":
        self.rt = Realtime(
            self.client, self.identity,
            state_fn=self.box.get,
            on_assignment=self._on_assignment,
            on_command=self._on_command,
            on_notify=self._on_notify,
            logger=self.log,
            interval=interval,
            # 本轮正在跑、中途插不了队，所以不要「一有待领任务就报一声」——
            # 那会变成每次状态同步都喊一句的噪音（原因见 Realtime._apply）。
            notify_on_pending_jobs=False,
        ).start()
        self.box.set(state="idle")
        return self

    def stop(self) -> None:
        if self.rt:
            self.rt.stop()
            self.rt = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # ---------------------------------------------------------- 回调

    def _on_assignment(self, assignment, switch_needed, reason) -> None:
        """心跳带回来的指派。存到内存 + 落盘，供本轮/下轮使用。"""
        if assignment:
            self.assignment = assignment
            write_assignment_cache(assignment, ASSIGN_CACHE)
        if switch_needed and reason:
            self.log("  · 后台要求切换账号/角色：%s" % reason)
            self.box.set(note="待切换：%s" % reason)

    def _on_notify(self, what: str, msg: Dict[str, Any]) -> None:
        """后台推过来的通知。

        · "jobs"   —— 后台派了新任务。**本轮正在跑、插不了队**，所以只记一笔；
                      这条任务会留在服务端队列里，由常驻代理或下一档接手
                      （常驻 agent.py 收到同一条通知会立刻起一轮 run_daily）。
        · "config" —— 远端配置变了。本轮不重拉（跑到一半换配置会让本轮结果
                      前后不一致），下一轮自然会拉到新版本。
        · 其它      —— 只是提醒本端同步一次状态（由 Realtime 内部处理）。
        """
        if what == "jobs":
            self.jobs_seen += 1
            job = msg.get("job")
            self.log("  · 后台派了新任务%s；本轮正在跑，跑完后它仍在队列里等下一档"
                     % ((" #%s" % job) if job else ""))
            self.box.set(note="后台又派了任务，排在队列里")
        elif what == "config":
            self.log("  · 后台配置已更新到 v%s（本轮沿用已拉到的版本，下一轮生效）"
                     % msg.get("version", "?"))

    def _on_command(self, cmd: Dict[str, Any]):
        """执行后台下发的一次性指令，返回 (ok, message, data)。"""
        kind = cmd.get("kind") or "probe"
        self.log("  · 收到后台指令：%s" % kind)

        # ★「切换」会去动游戏界面（点按钮、切角色）。本机正在跑任务时干这件事，
        #  两边的判断会互相打架（任务那边以为还在主城，其实已经被切走了）。
        #  所以忙的时候**不硬闯**，回报 BUSY 让服务端把指令放回队列，
        #  等本轮跑完自然会再取走执行 —— 管理端那一次点击不会白点。
        #  探测是只读的（截屏 + OCR），半路看一眼界面本来就有用，所以放行。
        if kind == "switch" and self.box.get().get("busy"):
            return False, (BUSY_PREFIX + "本机正在跑任务，切换会与任务抢游戏界面，"
                                        "已放回队列等本轮结束"), {"deferred": True}

        if kind == "probe":
            ui = self._ui_obj()
            if ui is None:
                return False, "本机还没连上模拟器/ADB，探测不了", {}
            info = describe_screen(ui)
            masked = None
            try:
                from stzb.account import find_masked
                items, _ = ui.ocr("probe_acct")
                masked = find_masked(items)
            except Exception:
                pass
            msg = "读到 %d 行文字" % info.get("text_count", 0)
            if masked:
                msg += "，当前账号 %s" % masked
            return True, msg, info

        if kind == "switch":
            ui = self._ui_obj()
            if ui is None:
                return False, "本机还没连上模拟器/ADB，切换不了", {}
            target = self.assignment or read_assignment_cache(ASSIGN_CACHE)
            if not target:
                return False, "后台还没给这台客户端指派账号/角色", {}
            r = ensure_target(ui, target, log=self.log)
            self.box.patch_current(masked=r.masked or None, role=r.role or None,
                                   switched_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            return r.ok, ("切换成功：%s" % r.reason) if r.ok else ("切换失败：%s" % r.reason), \
                r.to_dict()

        return False, "不认识的指令：%s" % kind, {}

    # ---------------------------------------------------------- Ui 工厂

    def _ui_obj(self):
        if self._ui is not None:
            return self._ui
        if self._make_ui is None:
            return None
        try:
            self._ui = self._make_ui()
        except Exception as e:
            self.log("  ! 创建 Ui 失败：%r" % (e,))
            return None
        return self._ui


def _state_reporter(client, cfg, slot: str, dry_run: bool, identity) -> Callable[[], Dict[str, Any]]:
    """拼一个「当前状态」函数给心跳线程调用。"""
    cloud = cfg.get("cloud") or {}
    base_extra = {"slot": slot, "dry_run": dry_run,
                  "base_url": cloud.get("base_url") or ""}

    def fn() -> Dict[str, Any]:
        return {"mode": "managed", "extra": base_extra}

    return fn


def cmd_upload_last(cfg) -> int:
    """只补传本地最近一份报告，不跑任务。网络恢复后用它兜底。"""
    print("· 补传本地最近一份报告…")
    got = load_last_report(REPORT_DIR)
    if not got:
        print("!! logs/reports/latest.json 不存在，没有可补传的东西")
        return 1
    stub, paths = got
    print("· 本地报告：%s" % (paths.get("html") or "（没有 html）"))
    print("· 轮次：%s 档位 %s" % (stub._data.get("started_at"), stub.slot))
    if not _cloud_ready(cfg, print):
        print("!! 还没绑定后端。双击 config_tool.bat → 「后端托管」填地址与令牌，")
        print("   或设环境变量 STZB_CLOUD_BASE_URL / STZB_CLOUD_TOKEN。")
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


def _do_cleanup(cfg, log, *, skip: bool = False, force: bool = False) -> dict:
    """按配置清理过期日志/截图。**任何异常都不抛**，绝不影响任务本身。

    · skip  —— `--no-cleanup`：本轮不清理
    · force —— `--cleanup-only`：无视 `logging.cleanup_enabled` 开关强制执行

    为什么放在「写报告之后」：报告是自包含 HTML（截图已 base64 内嵌），
    先写报告再删源图，绝不可能把刚跑完这轮的证据删掉。
    """
    if skip:
        log("· 按 --no-cleanup 跳过清理（%s）" % summary_line(ROOT))
        return {}
    enabled = True
    try:
        enabled = bool(cfg.get("logging.cleanup_enabled", True))
    except Exception:
        enabled = True
    if not (enabled or force):
        log("· 配置里关闭了自动清理（logging.cleanup_enabled=false），跳过")
        return {}
    try:
        p = parse_cleanup_cfg(cfg, ROOT)
        keep = p["keep_days"]
        log("· 清理策略：报告/日志保留 %s 天，截图保留 %s 天，截图上限 %s MB"
            % (keep if keep > 0 else "不限",
               (p["shots_keep_days"] if p["shots_keep_days"] is not None
                else (keep if keep > 0 else "不限")),
               p["shots_max_mb"] if p["shots_max_mb"] > 0 else "不限"))
        res = cleanup_by_days(ROOT, keep_days=keep,
                              shots_keep_days=p["shots_keep_days"],
                              shots_max_mb=p["shots_max_mb"],
                              log_keep_days=p["log_keep_days"],
                              diag=p["diag"], logger=log)
        log("· 清理后占用：%s" % summary_line(ROOT))
        return res
    except KeyboardInterrupt:
        log("  ! 清理被 Ctrl-C 中断")
        return {}
    except BaseException as e:
        # ★ 接 BaseException 而不是 Exception —— 本函数的契约是「任何异常都不抛」，
        #   而 SystemExit 之类**不属于 Exception**，实测能从这里穿出去、把整个收尾
        #   （写 last_run.json / 关模拟器 / 停心跳）带走。收尾的唯一职责就是把账记完，
        #   所以除了「用户按 Ctrl-C」以外，什么都不许中断它。
        log("  ! 清理过程异常：%r（不影响本轮结果）" % (e,))
        return {}


def _do_upload(cfg, report, paths, client, job, log, disabled=False, reason="",
               identity=None):
    """上传结果 + 给待执行任务回执。**任何失败都不抛异常**，也不影响本机报告。

    这是刻意的：上报告是尽力而为。网络断了、服务器挂了，已经跑完的任务不能白跑 ——
    本机 logs/reports/ 里那份永远完整，网络恢复后用 --upload-last 补传即可。

    `disabled`（没绑后端 / 显式 --no-upload）或 `client is None`（托管但初始化失败）时
    这里直接返回，**一个字节都不往外发**。
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
                                            .get("upload_shots_per_task", 4)),
                         identity=identity)
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
    ap.add_argument("--no-cleanup", action="store_true",
                    help="本轮跑完不清理过期日志/截图（默认会按配置自动清理）")
    ap.add_argument("--cleanup-only", action="store_true",
                    help="只做一次清理然后退出，不跑任务")
    ap.add_argument("--no-remote-config", action="store_true", help="不从后端拉配置，只用本地")
    ap.add_argument("--no-switch", action="store_true",
                    help="不做账号/角色切换，用当前已经在线的账号直接跑")
    ap.add_argument("--upload-last", action="store_true",
                    help="只把本机最近一份报告补传到后端，不跑任务")
    ap.add_argument("--job-only", action="store_true",
                    help="只为「后台待执行队列」里的任务跑一轮；队列空就立刻退出，"
                         "绝不退化成按档位的常规运行（常驻代理收到推送时用它）")
    ap.add_argument("--whoami", action="store_true",
                    help="打印本机的客户端标识（后台靠它认机器）")
    args = ap.parse_args()

    if args.list:
        for k, m in TASKS.items():
            print("  %-10s %-28s 档位=%s" % (k, m["name"], ",".join(m["at"])))
        return 0

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)
    cfg = load()
    _apply_cloud_env(cfg)          # 允许用环境变量覆盖后端地址/令牌（敏感值不必落盘）

    # 截图存哪儿：`logging.save_screens=false` 时改存系统临时目录 ——
    # 报告仍照常内嵌（它读的是内存里的图），但不再往 logs/shots 里堆。
    # 这样「不想留源图」的人也有得选，而不用为此关掉整个截图能力。
    shot_dir = SHOT_DIR
    if not bool(cfg.get("logging.save_screens", True)):
        shot_dir = os.path.join(tempfile.gettempdir(), "stzb_shots_tmp")
        print("· 配置 logging.save_screens=false：截图只存临时目录（%s），报告不受影响"
              % shot_dir)
    os.makedirs(shot_dir, exist_ok=True)

    if args.status:
        return cmd_status(cfg)

    if args.cleanup_only:
        print("清理前：%s" % summary_line(ROOT))
        _do_cleanup(cfg, print, force=True)
        print("清理后：%s" % summary_line(ROOT))
        return 0

    if args.whoami:
        ident = Identity.load(CLIENT_STATE)
        print("客户端标识（uid）: %s" % ident.uid)
        print("机器名            : %s" % ident.host)
        print("首次登记时间      : %s" % (ident.created_at or "—"))
        print("")
        print("后台「客户端」页上显示的就是这台机器。")
        print("标识文件：%s" % CLIENT_STATE)
        print("想重新登记（比如换了台机器、要复用旧记录）：删掉上面这个文件即可。")
        return 0

    if args.upload_last:
        return cmd_upload_last(cfg)

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

    # ------------------------------------------------ 后端绑定 + 拉配置 / 领任务
    # 必须放在「今天这一档跑过没」的判断**之前**：后端排的请求优先级最高，
    # 哪怕今天已经跑过，只要有人在控制台点了一次「执行」，就要照跑。
    mode = resolve_mode(cfg, False, log)
    managed = (mode == MODE_MANAGED)
    log("· 后端：%s" % describe_mode(cfg, mode))
    # 跳过上传时要说清是「没绑后端」还是「命令行显式要求」，否则日志里看不出区别
    upload_skip_reason = "未绑定后端" if not managed else "按 --no-upload 跳过上传"

    if args.job_only and not managed:
        log("!! --job-only 需要连后端才能领到任务，当前还没绑定后端 → 直接退出")
        return 0

    client = None
    job = None
    identity = Identity.load(CLIENT_STATE)
    rt_session: Optional[RealtimeSession] = None
    assignment: Dict[str, Any] = {}
    if managed:
        try:
            client = CloudClient(base_url=cfg["cloud"]["base_url"],
                                 token=cfg["cloud"]["token"],
                                 timeout=int(cfg["cloud"].get("timeout", 90)),
                                 retries=int(cfg["cloud"].get("retries", 3)),
                                 logger=log, uid=identity.uid)
        except CloudError as e:
            log("  ! 后端客户端初始化失败：%s" % e)

    if client is not None:
        log("· 本机客户端标识：%s（%s）" % (identity.uid, identity.host))

        # 第一次心跳：同时把「我上线了」和「后台给我派了哪个账号」一次拿到
        ok, info = client.ping()
        if ok:
            log("· 后端已连接：%s（远端配置版本 v%s）"
                % (cfg["cloud"]["base_url"], (info or {}).get("config_version", "?")))
            if (info or {}).get("task_paused"):
                log("  ! 后台把这台客户端设为「暂停派发」，本轮只跑命令行的任务")
        else:
            log("  ! 后端探活失败：%s（继续跑，跑完再试上传）" % info)

        # 起后台心跳线程：跑任务期间持续上报在线状态，并接收后台指令
        rt_session = RealtimeSession(client, identity, mode, log)
        rt_session.bind_ui_factory(lambda: Ui(
            Device(adb=cfg.get("device.adb") or DEFAULT_ADB,
                   serial_candidates=cfg.get("device.serial_candidates"),
                   shot_dir=shot_dir),
            cfg, logger=log, dry_run=True))
        try:
            rt_session.start()
            log("· 已开始后台实时会话（长连接优先；连不上会自动退回每 %d 秒心跳）"
                % (rt_session.rt.interval if rt_session.rt else 30))
        except Exception as e:
            log("  ! 心跳线程启动失败：%r（不影响任务）" % (e,))
            rt_session = None

        # 心跳响应里带的指派：这是「后端设置账号角色 → 客户端自动切换」的数据来源
        if isinstance(info, dict) and info.get("assignment"):
            assignment = info["assignment"]
            write_assignment_cache(assignment, ASSIGN_CACHE)
            log("· 后台指派：%s%s"
                % (assignment.get("label") or assignment.get("masked") or "?",
                   (" / " + assignment["role"]) if assignment.get("role") else ""))
            if info.get("switch_needed"):
                log("  · 后台要求切换：%s" % info.get("switch_reason") or "")
        else:
            # 后端没指派（或连不上）→ 用上次缓存的目标，保证断网也知道该用哪个账号
            cached = read_assignment_cache(ASSIGN_CACHE)
            if cached:
                assignment = cached
                log("· 使用上次缓存的指派：%s%s"
                    % (cached.get("label") or cached.get("masked") or "?",
                       (" / " + cached["role"]) if cached.get("role") else ""))

        if cfg["cloud"].get("pull_config", True) and not args.no_remote_config:
            apply_remote(cfg, client, REMOTE_STATE, logger=log)
        elif args.no_remote_config:
            log("· 按 --no-remote-config 跳过远端配置")

        if cfg["cloud"].get("pull_jobs", True):
            job = pick_job(client, logger=log)
        if args.job_only and not job:
            # ★ 常驻代理收到「后台派了任务」的推送时会用 --job-only 起一轮。
            #   队列空（任务被别的客户端抢走 / 已被取消）就**必须立刻退出** ——
            #   否则会退化成「按当前档位跑一遍常规任务」，那就等于凭空多跑一轮，
            #   还会重复领奖。这是这个开关存在的唯一理由。
            log("· 按 --job-only：后台待执行队列里没有属于本机的任务，直接退出")
            return 0
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

    # ------------------------------------------------ 角色「执行任务模式」
    # 后端可以给**每个角色**单独指定「跑哪些任务 / 在哪些档位跑 / 是否暂停」。
    # 优先级：控制台手动派的待执行任务（job）> 角色任务模式 > 全局配置与档位。
    #
    # 为什么 job 优先：那是「人在控制台点了执行，现在就要跑」，
    # 角色的日常计划（比如「只在 00:00 档跑」）不该把它拦住。
    if assignment and not job:
        tp = assignment.get("task_plan")
        if isinstance(tp, dict) and tp:
            role_name = assignment.get("role") or "?"
            if tplan.is_paused(tp):
                log("· 角色「%s」的执行任务模式是「暂停执行」→ 本轮不跑任何任务"
                    % role_name)
                if rt_session:
                    rt_session.box.set(state="idle", busy=False,
                                       note="角色已设为暂停执行")
                return 0
            if not tplan.allows_slot(tp, slot):
                log("· 角色「%s」只在 %s 档运行，当前是 %s 档 → 本轮跳过"
                    % (role_name, "、".join(tplan.normalize(tp)["slots"]), slot))
                if rt_session:
                    rt_session.box.set(state="idle", busy=False,
                                       note="不在该角色的运行档位")
                return 0
            picked = tplan.selected_tasks(tp, cfg.get("tasks"))
            if picked is not None:
                if not picked:
                    log("  ! 角色「%s」的执行任务模式是「自定义」，但一个任务都没勾选"
                        % role_name)
                    log("    到后台「角色执行」页给它勾上要跑的任务（或改回「跟随后端全局配置」）。")
                    if rt_session:
                        rt_session.box.set(state="idle", busy=False,
                                           note="角色任务模式未勾选任何任务")
                    return 0
                only = list(picked)
                force = True        # 任务范围被收窄了，不再看「今天这一档跑过没」
                log("· 按角色执行任务模式限定本轮任务：%s（%s）"
                    % ("、".join(only), tplan.summary_text(tp)))

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
    report = RunReport(ROOT, shot_dir, slot=slot, dry_run=dry_run, logger=log)
    rlog = Tee(log, report.note)          # 启动阶段的日志同时进报告
    emu_cfg = cfg.get("emulator", {}) or {}
    pkg = cfg.get("device.package") or GAME_PKG
    emu = make_emulator(cfg, rlog)

    if assignment:
        report.account_label = assignment.get("label") or assignment.get("masked") or ""
        report.role_label = assignment.get("role") or ""

    report.env_info(模拟器="MuMu 12（vmindex=%s）" % emu_cfg.get("vmindex", 0),
                    启动时间=report.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                    运行模式=describe_mode(cfg, mode),
                    档位=slot,
                    模式="预演（不点击）" if dry_run else "实际执行",
                    任务范围=only_label),
    if assignment:
        report.env_info(**{"目标账号": assignment.get("label") or assignment.get("masked") or "—",
                           "目标角色": assignment.get("role") or "（不限）"})
    if identity.uid:
        report.env_info(客户端标识=identity.uid)

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
        _do_cleanup(cfg, rlog, skip=args.no_cleanup)
        _do_upload(cfg, report, p, client, job, rlog,
                   disabled=(args.no_upload or not managed), reason=upload_skip_reason,
                   identity=identity)
        return code

    # ---------------------------------------------------------------- 1. 模拟器
    if rt_session:
        rt_session.box.set(state="starting", busy=True, note="正在准备模拟器")
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
                 shot_dir=shot_dir)
    if not dev.connect(retries=6, wait=2.0):
        return _bail(3, "ADB 连接失败")
    rlog("· ADB 已连接：%s" % dev.serial)
    report.env_info(ADB设备=dev.serial)
    if rt_session:
        rt_session.box.set(state="starting", note="ADB 已连接，准备打开游戏")
        rt_session.box.patch_device(emulator_running=True, adb_serial=dev.serial)

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
    if rt_session:
        rt_session.box.set(state="starting", note="游戏已启动")
        rt_session.box.patch_device(game_running=True)

    ui = Ui(dev, cfg, logger=rlog, dry_run=dry_run)
    if rt_session:
        rt_session._ui = ui          # 后台探测指令直接复用这个 Ui，不用另建

    # ---------------------------------------------------------------- 3.5 账号/角色切换
    # 放在 boot() 之前：切换必须在**登录页**完成（「点击换区」是登录页的入口），
    # 一旦 boot() 点了「开始游戏」进了主城，就得先退出来才能切。
    #
    # 开关优先级：命令行 --no-switch（临时）> config.account.* （本机长期设置）。
    # account 段是**本机独占**的（不进后端可下发白名单），见 stzb/remote_config.py。
    acct_cfg = cfg.get("account") or {}
    want_switch = bool(assignment) and not args.no_switch \
        and acct_cfg.get("enabled", True) is not False
    if want_switch:
        # 细粒度开关：只切角色（比如多开但共用账号）时能省掉一次登录跳转
        if not acct_cfg.get("switch_account", True):
            assignment = {k: v for k, v in assignment.items() if k != "masked"}
            rlog("· 本机设置：不切账号，只校验角色")
        if not acct_cfg.get("switch_role", True):
            assignment = {k: v for k, v in assignment.items() if k != "role"}
            rlog("· 本机设置：不切角色，只校验账号")

    if want_switch and (assignment.get("masked") or assignment.get("role")):
        if rt_session:
            rt_session.box.set(state="switching", note="正在按后台指派切换账号/角色")
        rlog("· 按后台指派校验账号 / 角色…")
        t_sw = time.time()
        sw = ensure_target(ui, assignment, log=rlog, dry_run=dry_run)
        report.env_info(切换结果="%s（%.0f 秒）" % (
            "✓ " + sw.reason if sw.ok else "× " + sw.reason, time.time() - t_sw))
        if not sw.ok:
            # 切换失败**不能硬着头皮跑** —— 那是别人的账号，跑出来的任务全错。
            rlog("!! 账号/角色切换失败，本轮中止（避免跑错账号）")
            if rt_session:
                rt_session.box.set(state="error",
                                   note="切换失败：%s" % sw.reason)
                rt_session.box.patch_current(switched_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            return _bail(5, "账号/角色切换失败：%s" % sw.reason)
        if rt_session:
            rt_session.box.patch_current(
                masked=sw.masked or assignment.get("masked"),
                role=sw.role or assignment.get("role"),
                account_label=assignment.get("label"),
                switched_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    elif assignment and (assignment.get("masked") or assignment.get("role")):
        rlog("· 已按本机设置跳过账号/角色切换（--no-switch 或 config.account.enabled=false）")

    # ---------------------------------------------------------------- 4. 进主城
    t0 = time.time()
    if rt_session:
        rt_session.box.set(state="starting", note="正在进入主城")
    if not ui.boot():
        return _bail(4, "进主城失败")
    report.env_info(进入主城耗时="%.0f 秒" % (time.time() - t0))

    # ---------------------------------------------------------------- 5. 任务
    if rt_session:
        rt_session.box.set(state="running", busy=True,
                           note="正在跑 %s 档任务" % slot)
    started = time.time()
    res = run_all(ui, cfg, slot, only=only, logger=log, report=report)

    log("-" * 70)
    for k, v in res.items():
        log("  %-10s %s" % (k, "OK" if v else "失败/跳过"))
    log("耗时 %.1f 秒；源截图存于 %s" % (time.time() - started, shot_dir))
    if rt_session:
        rt_session.box.set(state="idle", busy=False,
                           note="%s 档跑完，成功 %d / 共 %d"
                                % (slot, sum(1 for v in res.values() if v), len(res)),
                           last_run_at=dt.datetime.now().isoformat(timespec="seconds"))

    # ---------------------------------------------------------------- 5. 报告
    report.env_info(总耗时="%.1f 秒" % (time.time() - t_run0))
    paths = report.write_all(REPORT_DIR, cfg.get("logging.keep_days", 14))
    log("=" * 70)
    log("执行结果：")
    for line in report.summary_text().splitlines():
        log("  " + line)
    log("报告（含截图）：%s" % paths["html"])
    log("结构化结果：%s" % paths["latest_json"])

    # ------------------------------------------------------- 5.5 清理过期文件
    # 放在写报告之后：报告已把本轮截图内嵌进去，再删源图不会丢证据。
    report.env_info(磁盘占用=summary_line(ROOT))
    # ★ 第二道防线（`_do_cleanup` 自己已经扛住了）。
    #   这里必须**连 BaseException 一起接住**，因为收尾链的硬要求是
    #   「无论如何都要把 last_run.json 写上、把模拟器关掉、把心跳停掉」——
    #   少了它，下次调度会以为今天没跑，**重复跑一整轮、重复领奖**，
    #   这比直接失败更难发现。兜极端情况：磁盘满到连 log 自己都写不出去、
    #   或运行环境在删除路径上插了钩子直接 SystemExit。
    #   只放行 KeyboardInterrupt：Ctrl-C 是用户要停，那时该停。
    try:
        _do_cleanup(cfg, log, skip=args.no_cleanup)
    except KeyboardInterrupt:
        log("  ! 清理被 Ctrl-C 中断")
    except BaseException as e:                       # noqa: BLE001 收尾不容中断
        log("  ! 清理异常（已忽略，不影响本轮结果）：%r" % e)

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
    _do_upload(cfg, report, paths, client, job, log,
               disabled=(args.no_upload or not managed), reason=upload_skip_reason,
               identity=identity)

    if not only:
        if st.get("date") != today:
            st = {"date": today, "slots": []}
        if slot not in st["slots"]:
            st["slots"].append(slot)
        st["last_finish"] = dt.datetime.now().isoformat(timespec="seconds")
        st["last_report"] = paths["html"]
        save_state(st)

    # 收尾前把「跑完了」这个状态刷上去，再停心跳 ——
    # 否则后台看到的最后一拍还停在「正在跑」，要等 90 秒才判离线。
    if rt_session:
        rt_session.box.set(state="idle", busy=False,
                           note="本轮结束（%s 档）" % slot,
                           last_run_at=dt.datetime.now().isoformat(timespec="seconds"))
        try:
            rt_session.rt.beat_now()    # 立刻补一拍，后台马上就看到「空闲了」
        except Exception:
            pass
        rt_session.stop()
        log("· 后台心跳已停止。")

    if args.open_report:
        try:
            os.startfile(paths["html"])          # noqa: S606  (Windows 专用)
        except Exception as e:
            log("  （自动打开报告失败：%r）" % e)

    return 0 if all(res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
