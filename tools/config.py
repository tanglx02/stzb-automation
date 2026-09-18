# -*- coding: utf-8 -*-
"""本机配置工具 —— 把 config.json 里所有能配的东西收成一个工具。

**这个工具是可选的。** 它只做一件事：读写项目根目录的 `config.json`。
不用它、删掉它，脚本照样跑 —— 默认值都在 `stzb/config.py` 的 DEFAULTS 里。

两种用法：

  交互式菜单（推荐，双击 `config_tool.bat` 或直接跑）：
      python tools/config.py

  命令行（给脚本 / 自动化用，也可用于远程指导）：
      python tools/config.py show                     看全部配置（标注来源）
      python tools/config.py show --section cloud
      python tools/config.py get cloud.enabled
      python tools/config.py set recruit.half_price false
      python tools/config.py set device.adb "D:\\MuMu\\adb.exe"
      python tools/config.py reset recruit.half_price 恢复该项为出厂默认
      python tools/config.py bind --url https://xx --token stzb_xx   绑定并当场验证
      python tools/config.py unbind                   解除绑定，回到独立运行
      python tools/config.py verify                   只验证后端连通性
      python tools/config.py backup                   手动备份 config.json
      python tools/config.py restore                  从备份恢复（列出可选）

设计要点：
  * 改配置走「读原始 JSON → 只改目标键 → 原子写回」，**保留文件里的 `_说明` 注释键**，
    也不把默认值全量写进文件（免得以后改默认值改不动）。
  * 写入前自动备份到 `state/config_backup/`，保留最近 10 份。
  * 会提示哪些项**正在被服务端接管** —— 那些项改本地是没用的，改了也会被远端覆盖。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb import config as cfgmod                     # noqa: E402
from stzb.cloud import CloudClient, CloudError        # noqa: E402
from stzb.remote_config import MANAGED_SECTIONS       # noqa: E402

CONFIG_PATH = cfgmod.CONFIG_PATH
STATE_DIR = os.path.join(ROOT, "state")
REMOTE_STATE = os.path.join(STATE_DIR, "remote_config.json")
BACKUP_DIR = os.path.join(STATE_DIR, "config_backup")
KEEP_BACKUPS = 10

LINE = "=" * 66
THIN = "-" * 66


# --------------------------------------------------------------------------- 字段定义

@dataclass
class Field:
    """一个可配置项。菜单、校验、取值、帮助文字全部由它驱动。"""
    path: str
    label: str
    kind: str                     # bool | int | float | str | url | path | choice | list
    help: str = ""
    choices: Sequence[str] = ()
    lo: Optional[float] = None
    hi: Optional[float] = None
    writable: bool = True         # False = 只读展示（比如 cloud 段里给用户看的提示）

    @property
    def section(self) -> str:
        return self.path.split(".")[0]

    @property
    def managed(self) -> bool:
        """这一项会不会被服务端下发覆盖。"""
        return self.section in MANAGED_SECTIONS

    @property
    def default(self) -> Any:
        cur: Any = cfgmod.DEFAULTS
        for part in self.path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur


# 分组顺序 = 菜单顺序。每一组是一个菜单项。
GROUPS: List[Tuple[str, str, List[Field]]] = [
    ("cloud", "后端托管（是否绑定服务器）", [
        Field("cloud.enabled", "是否托管到服务器", "bool",
              "false = 独立运行（不连任何外部服务，报告只留本机）；true = 托管"),
        Field("cloud.base_url", "服务器地址", "url",
              "例如 https://stzb.example.com，不要带结尾斜杠"),
        Field("cloud.token", "采集端令牌", "str",
              "在服务器控制台的「设置」页可以轮换，形如 stzb_xxxx"),
        Field("cloud.timeout", "请求超时（秒）", "int", lo=5, hi=600),
        Field("cloud.retries", "失败重试次数", "int", lo=0, hi=10),
        Field("cloud.pull_config", "允许服务端下发业务配置", "bool",
              "只影响 tasks / 各任务参数 / safety；device、emulator、cloud、logging 永远以本机为准"),
        Field("cloud.pull_jobs", "启动时领取控制台排的待执行任务", "bool"),
        Field("cloud.upload_shots_per_task", "每个任务上传几张截图", "int",
              "0 = 不上传截图；截图会先缩到 1000px 再传", lo=0, hi=20),
    ]),

    ("device", "模拟器与 ADB", [
        Field("device.adb", "adb.exe 路径", "path",
              r"MuMu 的在 C:\Program Files\Netease\MuMu\nx_main\adb.exe"),
        Field("device.serial_candidates", "ADB 候选地址（按顺序试）", "list",
              "一行一个，形如 127.0.0.1:7555。实测同一台模拟器会同时出现在 127.0.0.1:7555 和 emulator-5554"),
        Field("device.package", "游戏包名", "str", "率土之滨是 com.netease.stzb.netease"),
        Field("emulator.manager", "MuMuManager.exe 路径", "path",
              r"用来启停模拟器，在 MuMu\nx_main\MuMuManager.exe"),
        Field("emulator.vmindex", "虚拟机索引", "int",
              "MuMu 里第一台是 0；只有开了多台才需要改", lo=0, hi=9),
        Field("emulator.startup_timeout", "等模拟器就绪上限（秒）", "int",
              "本机实测 11 秒就绪，留足余量即可", lo=30, hi=1800),
        Field("emulator.shutdown_after", "跑完是否关闭模拟器", "choice",
              "auto = 只关「本次脚本自己启动的」（推荐，不会打断你在玩的游戏）",
              choices=("auto", "always", "never")),
        Field("emulator.cold_restart", "每次都先完全关掉再冷启动", "bool",
              "状态最干净，代价是每轮多花 1~2 分钟"),
    ]),

    ("tasks", "任务开关（六个任务跑不跑）", [
        Field("tasks.gongpin", "贡品礼包 / 月卡礼包", "bool"),
        Field("tasks.shuishou", "内政税收", "bool"),
        Field("tasks.shijing", "内政市井", "bool"),
        Field("tasks.yanwu", "内政演武（扫荡奖励）", "bool"),
        Field("tasks.texing", "内政特性", "bool", "只在 12:00 档跑"),
        Field("tasks.recruit", "招募", "bool"),
    ]),

    ("recruit", "招募参数（每天 2 免费 + 2 半价）", [
        Field("recruit.free", "抽免费的", "bool",
              "游戏 00:00 和 12:00 各刷一次免费，每天共 2 次；本任务两个档位都会跑"),
        Field("recruit.half_price", "抽半价", "bool",
              "00:00 和 12:00 各刷一次，每天共 2 次（100 虎符/次）。"
              "安全阀：必须同时满足「有打折标记」且「价格 ≤ 下面那个上限」才抽"),
        Field("recruit.half_price_max_hufu", "愿意为半价付的虎符上限", "int",
              "读到原价 200 会直接拒抽", lo=0, hi=1000),
        Field("recruit.auto_buy_hufu", "虎符不够时用玉符按 1:1 兑换", "bool"),
        Field("recruit.hufu_buy_max", "每次最多兑换多少虎符", "int", lo=0, hi=10000),
    ]),

    ("shijing", "市井参数", [
        Field("shijing.free_item", "领宝物商队的免费物品", "bool"),
        Field("shijing.buy_materials", "要用铜钱买的材料（一行一个）", "list",
              "要写游戏里的准确名字；脚本用模糊匹配，OCR 错字也能对上"),
        Field("shijing.material_pay_copper_only", "只接受铜钱价", "bool",
              "价格图标是绿色（玉符）就不买"),
        Field("shijing.material_max_copper", "单笔铜钱上限", "int", lo=0, hi=10000000),
    ]),

    ("shuishou", "税收参数", [
        Field("shuishou.max_times", "征收次数上限", "int",
              "游戏每天给 3 次；脚本永远不会点花虎符的「强征」", lo=0, hi=10),
    ]),

    ("texing", "特性参数", [
        Field("texing.free_only", "只抽免费的那张", "bool", "花玉符的一律不抽"),
        Field("texing.wait_free_seconds", "免费次数还剩不到这么多秒就等一下", "int",
              "兜住 12:00 档跑得比刷新早几分钟的情况", lo=0, hi=3600),
    ]),

    ("yanwu", "演武参数", [
        Field("yanwu.daily_sweep", "每天领一次「扫荡奖励」", "bool",
              "纯领取，不花任何货币；刷新点是每天 00:00"),
        Field("yanwu.wait_free_seconds", "离可领取只剩这么多秒就等一下", "int", lo=0, hi=3600),
    ]),

    ("gongpin", "贡品礼包", [
        Field("gongpin.enabled", "进去核对月卡礼包", "bool",
              "该页面实测没有领取按钮，每日玉符是随月卡自动发的；"
              "脚本进去核对并如实报告，不会把说明浮层误报成领取成功"),
    ]),

    ("safety", "安全阀", [
        Field("safety.never_tap", "绝不点击的词（一行一个）", "list",
              "要点的按钮名里出现这些词就放弃点击。别加「充值」「购买」这类词，"
              "会误伤「充值好礼」页签和市井购物"),
        Field("safety.max_task_seconds", "单个任务超时上限（秒）", "int", lo=30, hi=3600),
        Field("safety.max_total_seconds", "一轮总超时上限（秒）", "int", lo=60, hi=7200),
        Field("safety.tap_delay", "每次点击后的等待（秒）", "float", lo=0, hi=5),
    ]),

    ("logging", "日志与报告", [
        Field("logging.save_screens", "保存每一步的截图", "bool",
              "关掉能省磁盘，但出问题时没法从截图倒查，建议保持开启"),
        Field("logging.keep_days", "本机报告保留天数", "int", "0 = 不清理", lo=0, hi=365),
    ]),
]

ALL_FIELDS: Dict[str, Field] = {f.path: f for _, _, fs in GROUPS for f in fs}


# --------------------------------------------------------------------------- 读写

def load_raw() -> Dict[str, Any]:
    """读 config.json 原始内容（不合并默认值，保留 _说明）。坏了也不崩。"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("!! config.json 解析失败（%s），将按空配置处理。" % e)
        return {}


def save_raw(data: Dict[str, Any]) -> None:
    """原子写回：先写临时文件再替换，避免中断留下半截文件。"""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def effective():
    """合并默认值之后的配置（只读展示用）。"""
    return cfgmod.load()


def get_effective(path: str) -> Any:
    return effective().get(path)


def is_explicit(path: str) -> bool:
    """这个键是否在 config.json 里显式写了（没写就是走内置默认值）。"""
    cur: Any = load_raw()
    parts = path.split(".")
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    return isinstance(cur, dict) and parts[-1] in cur


def set_value(path: str, value: Any, quiet: bool = False) -> None:
    data = load_raw()
    cur = data
    parts = path.split(".")
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    old = cur.get(parts[-1], "<未设置>")
    cur[parts[-1]] = value
    backup()
    save_raw(data)
    if not quiet:
        print("  ✓ 已保存  %s" % path)
        print("      %s  →  %s" % (fmt(old), fmt(value)))


def unset_value(path: str, quiet: bool = False) -> bool:
    """把某个键从文件里删掉，让它回到内置默认值。返回是否真的删了。"""
    data = load_raw()
    cur = data
    parts = path.split(".")
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            if not quiet:
                print("  （本来就没显式设置，已在用默认值）")
            return False
        cur = cur[p]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        if not quiet:
            print("  （本来就没显式设置，已在用默认值）")
        return False
    old = cur.pop(parts[-1])
    backup()
    save_raw(data)
    if not quiet:
        print("  ✓ 已恢复默认  %s" % path)
        print("      %s  →  %s" % (fmt(old), fmt(ALL_FIELDS[path].default)))
    return True


# --------------------------------------------------------------------------- 备份

def backup() -> Optional[str]:
    if not os.path.exists(CONFIG_PATH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, "config_%s.json" % stamp)
    try:
        shutil.copy2(CONFIG_PATH, dst)
    except Exception:
        return None
    olds = sorted(f for f in os.listdir(BACKUP_DIR)
                  if f.startswith("config_") and f.endswith(".json"))
    for f in olds[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
        except OSError:
            pass
    return dst


def list_backups() -> List[str]:
    if not os.path.isdir(BACKUP_DIR):
        return []
    return sorted((f for f in os.listdir(BACKUP_DIR)
                   if f.startswith("config_") and f.endswith(".json")), reverse=True)


# --------------------------------------------------------------------------- 取值 / 校验

def fmt(v: Any) -> str:
    """给人看的显示形式。令牌只露头尾。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return "、".join(str(x) for x in v) if v else "（空）"
    if v is None:
        return "（未设置）"
    s = str(v)
    if s.startswith("stzb_") and len(s) > 12:
        return "%s…%s" % (s[:9], s[-4:])
    return s if s else "（空）"


def ask(prompt: str, default: str = "") -> Optional[str]:
    """读一行输入。空 = 用默认值；q = 取消返回上一层。Ctrl+C / EOF 也当取消。"""
    tip = " [%s]" % default if default != "" else ""
    try:
        raw = input("  %s%s> " % (prompt, tip)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if raw.lower() in ("q", "quit", "back", "返回"):
        return None
    return raw or default


def parse_bool(s: str) -> Optional[bool]:
    t = s.strip().lower()
    if t in ("1", "true", "yes", "y", "on", "是", "开", "启用"):
        return True
    if t in ("0", "false", "no", "n", "off", "否", "关", "停用"):
        return False
    return None


def parse_path(s: str) -> str:
    """路径去掉可能被误加的首尾引号。"""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s


def coerce(f: Field, raw: str) -> Tuple[bool, Any, str, bool]:
    """把用户输入的字符串转成正确类型。

    返回 (ok, value, error, soft)：
      soft=True 表示「值的格式没问题，但可能填错了」（比如路径不存在）。
      CLI 可以用 --force 放行，交互模式会追问一句确认。
      这样区分是有意的：把 `abc` 填进整数项是**错误**，不该允许绕过；
      而填一个还没装的路径是**可能有意为之**（比如准备先配好再装 MuMu）。
    """
    if f.kind == "bool":
        b = parse_bool(raw)
        if b is None:
            return False, None, "请输入 true/false（或 y/n、是/否、1/0）", False
        return True, b, "", False
    if f.kind == "int":
        try:
            n = int(str(raw).strip())
        except Exception:
            return False, None, "请输入整数", False
        if f.lo is not None and n < f.lo:
            return False, None, "不能小于 %s" % f.lo, False
        if f.hi is not None and n > f.hi:
            return False, None, "不能大于 %s" % f.hi, False
        return True, n, "", False
    if f.kind == "float":
        try:
            n = float(str(raw).strip())
        except Exception:
            return False, None, "请输入数字", False
        if f.lo is not None and n < f.lo:
            return False, None, "不能小于 %s" % f.lo, False
        if f.hi is not None and n > f.hi:
            return False, None, "不能大于 %s" % f.hi, False
        return True, n, "", False
    if f.kind == "choice":
        v = raw.strip()
        if v not in f.choices:
            return False, None, "只能是：%s" % " / ".join(f.choices), False
        return True, v, "", False
    if f.kind == "url":
        v = raw.strip().rstrip("/")
        if not v:
            return True, "", "", False
        if not v.lower().startswith(("http://", "https://")):
            return False, None, "必须以 http:// 或 https:// 开头", False
        u = urllib.parse.urlparse(v)
        if not u.netloc:
            return False, None, "地址不完整，缺少域名", False
        return True, v, "", False
    if f.kind == "path":
        v = parse_path(raw)
        if v and not os.path.exists(v):
            return False, v, "本机没有这个路径：%s" % v, True
        return True, v, "", False
    if f.kind == "list":
        text = raw.replace("、", "\n").replace(",", "\n").replace("，", "\n")
        items = [x.strip() for x in text.splitlines() if x.strip()]
        return True, items, "", False
    return True, raw.strip(), "", False


# --------------------------------------------------------------------------- 后端绑定

def describe_error(err: str) -> str:
    """把 HTTP/网络错误翻译成能照着做的提示。"""
    e = err or ""
    low = e.lower()
    if "401" in e:
        return "服务端拒绝了令牌（401）。请到控制台「设置」页核对令牌，或用 new-token 重新生成。"
    if "403" in e:
        return "服务端禁止访问（403）。检查反向代理是否拦了这个路径。"
    if "404" in e:
        return "服务器上没有这个接口（404）。地址多半写错了 —— 只填 https://域名，不要带路径。"
    if "429" in e:
        return "请求太频繁（429），稍后再试。"
    if "证书" in e or "certificate" in low or "ssl" in low:
        return ("TLS 证书校验失败。检查域名证书是否有效；"
                "本机没有 certifi 时也会这样，可执行 pip install certifi。")
    if "10061" in e or "refused" in low:
        return "连接被拒绝。确认服务器在跑、域名解析正确、防火墙放行了 443。"
    if "timed out" in low or "timeout" in low:
        return "连接超时。检查网络、域名解析，或服务器是否被墙/未启动。"
    if "getaddrinfo" in low or "name or service" in low:
        return "域名解析不了。检查 base_url 里的域名拼写。"
    return "请检查地址、令牌与服务器状态。"


def verify_backend(base_url: str, token: str, timeout: int = 15) -> Tuple[bool, str, Dict[str, Any]]:
    """连一次后端做完整校验。返回 (是否通过, 说明, 细节)。**不抛异常。**"""
    base_url = (base_url or "").strip().rstrip("/")
    token = (token or "").strip()
    if not base_url:
        return False, "没填服务器地址", {}
    if not token:
        return False, "没填采集端令牌", {}
    try:
        client = CloudClient(base_url=base_url, token=token,
                             timeout=timeout, retries=1, logger=lambda m: None)
    except CloudError as e:
        return False, str(e), {}

    ok, info = client.ping()
    if not ok:
        return False, "探活失败：%s\n      %s" % (info, describe_error(str(info))), {}
    if not isinstance(info, dict):
        return False, "服务端返回的内容不是预期格式：%r" % (info,), {}

    detail = {
        "app": info.get("app", "?"),
        "version": info.get("version", "?"),
        "config_version": info.get("config_version", 0),
        "allow_run_requests": info.get("allow_run_requests", False),
    }
    # 再拉一次配置，确认令牌有读配置的权限、且返回可解析
    ok2, payload = client.fetch_config(version=-1)
    if not ok2:
        return False, "探活通过，但拉配置失败：%s" % payload, detail
    if isinstance(payload, dict):
        detail["payload_sections"] = sorted((payload.get("payload") or {}).keys())
        detail["remote_note"] = payload.get("note") or ""
    return True, "连接正常", detail


# --------------------------------------------------------------------------- 展示

def mode_summary() -> Tuple[str, str]:
    """返回 (模式短名, 一句话说明)。"""
    cloud = effective().get("cloud") or {}
    if not cloud.get("enabled"):
        return "独立运行", "不连任何外部服务，报告只留在本机"
    if not cloud.get("base_url") or not cloud.get("token"):
        return "独立运行", "cloud.enabled 是 true，但地址或令牌没填全 → 自动按独立运行处理"
    return "后端托管", "地址 %s" % cloud.get("base_url")


def remote_override_note() -> str:
    """如果服务端下发过配置，提示哪些段正在被接管。"""
    try:
        with open(REMOTE_STATE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return ""
    ver = st.get("version")
    payload = st.get("payload") or {}
    if not ver or not payload:
        return ""
    secs = "、".join(sorted(payload.keys()))
    when = st.get("applied_at") or ""
    return ("⚠ 服务端已下发过配置（v%s%s），正在接管的段：%s\n"
            "  这些段改本机 config.json 是**没用的**，会被远端覆盖 —— 请到控制台改。"
            % (ver, ("，" + when) if when else "", secs))


def _char_width(ch: str) -> int:
    """一个字符占几列。中文/全角算 2 列，其余算 1 列。

    不能用 `str.ljust` / `%-46s` 排版：它们按**字符个数**补空格，
    而中文在终端里占两列，结果就是中英混排的表格永远对不齐。
    """
    o = ord(ch)
    if o < 0x1100:
        return 1
    if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
            or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F
            or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6
            or 0x20000 <= o <= 0x3FFFD):
        return 2
    return 1


def disp_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def pad(s: str, width: int) -> str:
    """按显示宽度右侧补空格。超宽就截断并加省略号。"""
    if disp_width(s) > width:
        out: List[str] = []
        w = 0
        for ch in s:
            d = _char_width(ch)
            if w + d > width - 1:
                break
            out.append(ch)
            w += d
        return "".join(out) + "…" + " " * max(0, width - w - 1)
    return s + " " * (width - disp_width(s))


def cmd_show(args) -> int:
    raw = load_raw()
    print(LINE)
    print("  本机配置  %s" % CONFIG_PATH)
    print(LINE)
    mode, why = mode_summary()
    print("  运行模式：%s  —— %s" % (mode, why))
    print("  文件状态：%s" % ("存在" if raw else "不存在（全部在用内置默认值）"))
    note = remote_override_note()
    if note:
        print()
        for ln in note.splitlines():
            print("  " + ln)
    print()

    only = args.section
    explicit_count = 0
    for sec, gtitle, fields in GROUPS:
        if only and sec != only:
            continue
        print("%s" % gtitle)
        for f in fields:
            val = get_effective(f.path)
            explicit_count += 1 if is_explicit(f.path) else 0
            mark = "本地" if is_explicit(f.path) else "默认"
            tag = "  ← 服务端可覆盖" if f.managed else ""
            print("  %s %s %s%s" % (pad(f.path, 34), pad(fmt(val), 40), mark, tag))
        print()
    if not only:
        print("共 %d 项，其中 %d 项在本地文件里显式设置过（其余走内置默认值）。"
              % (len(ALL_FIELDS), explicit_count))
        print('只看某一段：python tools/config.py show --section cloud')
        print("  段名：%s" % " / ".join(sec for sec, _, _ in GROUPS))
    return 0


def cmd_get(args) -> int:
    f = ALL_FIELDS.get(args.path)
    if not f:
        print("!! 不认识这个配置项：%s" % args.path)
        print("   可用项：python tools/config.py show")
        return 2
    v = get_effective(args.path)
    if args.json:
        print(json.dumps(v, ensure_ascii=False))
    else:
        print(fmt(v))
    return 0


def cmd_set(args) -> int:
    f = ALL_FIELDS.get(args.path)
    if not f:
        print("!! 不认识这个配置项：%s" % args.path)
        print("   可用项：python tools/config.py show")
        return 2
    if not f.writable:
        print("!! 这一项不能直接设置")
        return 2
    ok, value, err, soft = coerce(f, args.value)
    if not ok:
        if soft and args.force:
            ok = True
            print("  ! 已按 --force 跳过检查：%s" % err)
        else:
            print("!! %s" % err)
            if soft:
                print("   如果确实要这么填（比如准备之后才装模拟器），加 --force 跳过这个检查。")
            return 2
    if f.managed:
        note = remote_override_note()
        if note:
            print(note)
            print()
    set_value(f.path, value)
    if f.path == "cloud.enabled" and value:
        cloud = effective().get("cloud") or {}
        if not cloud.get("token"):
            print("  ! 还差一步：令牌没填，用 bind 命令或菜单里的「后端绑定」补上，"
                  "否则仍会按独立运行处理。")
    return 0


def cmd_reset(args) -> int:
    if args.path == "all":
        backup()
        save_raw({})
        print("  ✓ 已清空 config.json（全部回到内置默认值）；"
              "旧文件已备份到 %s" % BACKUP_DIR)
        return 0
    if args.path not in ALL_FIELDS:
        print("!! 不认识这个配置项：%s（想全部清空用 --path all）" % args.path)
        return 2
    unset_value(args.path)
    return 0


def cmd_bind(args) -> int:
    print(LINE)
    print("  绑定后端")
    print(LINE)
    cur = effective().get("cloud") or {}
    url = args.url or cur.get("base_url") or ""
    token = args.token or cur.get("token") or ""
    if not args.url and not url:
        print("!! 需要服务器地址。用 --url https://你的域名")
        return 2
    if not args.token and not token:
        print("!! 需要采集端令牌。用 --token stzb_xxxx")
        print("   令牌在服务器上首次启动时打印过一次；也可以执行")
        print("   docker compose exec app python -m app.cli new-token 重新生成。")
        return 2

    print("· 正在验证 %s …" % url)
    ok, msg, detail = verify_backend(url, token, timeout=args.timeout)
    if not ok:
        print("  ✗ %s" % msg)
        print()
        print("  没有写入任何配置。可以先自查：")
        print("    · 浏览器能打开 %s/healthz 吗" % url)
        print("    · 令牌是不是整串复制全了（形如 stzb_ 开头）")
        return 1

    print("  ✓ 连接正常")
    print("      服务端      : %s %s" % (detail.get("app"), detail.get("version")))
    print("      远端配置版本: v%s" % detail.get("config_version"))
    print("      可下发段    : %s" % ("、".join(detail.get("payload_sections") or []) or "（还没配过）"))
    if not detail.get("allow_run_requests"):
        print("      注意：服务端已关闭「触发任务」功能，脚本领不到待执行任务")
    print()
    set_value("cloud.base_url", url, quiet=True)
    set_value("cloud.token", token, quiet=True)
    set_value("cloud.enabled", True, quiet=True)
    print("  ✓ 已写入 config.json 并启用托管")
    print()
    print("  下次跑任务时会：拉远端配置 → 领待执行任务 → 跑完上传结果与截图。")
    print("  想确认：python run_daily.py --status")
    print("  想解绑：python tools/config.py unbind")
    return 0


def cmd_unbind(args) -> int:
    print("· 解除后端绑定，回到独立运行…")
    ok = False
    for p in ("cloud.enabled", "cloud.base_url", "cloud.token"):
        if is_explicit(p):
            unset_value(p, quiet=True)
            ok = True
    set_value("cloud.enabled", False, quiet=True)
    print("  ✓ cloud.enabled = false（并清掉了地址与令牌）" if ok
          else "  ✓ cloud.enabled = false")
    print()
    print("  从现在起脚本不连任何外部服务，报告只留在本机 logs\\reports\\。")
    print("  已经跑过的记录和报告不受影响。")
    return 0


def cmd_verify(args) -> int:
    cur = effective().get("cloud") or {}
    url = args.url or cur.get("base_url") or ""
    token = args.token or cur.get("token") or ""
    mode, why = mode_summary()
    print("当前模式：%s  —— %s" % (mode, why))
    if not url or not token:
        print("!! 还没绑定后端（缺地址或令牌）")
        return 2
    print("· 验证 %s …" % url)
    ok, msg, detail = verify_backend(url, token, timeout=args.timeout)
    if ok:
        print("  ✓ %s" % msg)
        print("      远端配置版本: v%s" % detail.get("config_version"))
        return 0
    print("  ✗ %s" % msg)
    return 1


def cmd_backup(args) -> int:
    p = backup()
    if not p:
        print("!! 没有 config.json 可备份")
        return 1
    print("  ✓ 已备份到 %s" % p)
    return 0


def cmd_restore(args) -> int:
    items = list_backups()
    if not items:
        print("!! 还没有任何备份（改配置时会自动备份）")
        return 1
    if not args.name:
        print("可选备份（新的在前）：")
        for i, f in enumerate(items, 1):
            print("  %d) %s" % (i, f))
        print()
        print("恢复：python tools/config.py restore --name %s" % items[0])
        return 0
    name = args.name
    if name.isdigit() and 1 <= int(name) <= len(items):
        name = items[int(name) - 1]
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(path):
        print("!! 找不到备份：%s" % name)
        return 2
    backup()                       # 覆盖前先把当前状态也留一份
    dst = json.load(open(path, encoding="utf-8"))
    save_raw(dst)
    print("  ✓ 已从 %s 恢复" % name)
    print("      （恢复前的配置也备份了，可再用 restore 回退）")
    return 0


# --------------------------------------------------------------------------- 交互菜单

def menu_edit_field(f: Field) -> None:
    while True:
        print()
        print(THIN)
        print("  %s" % f.label)
        print("  配置项：%s" % f.path)
        if f.help:
            print("  说明  ：%s" % f.help)
        if f.choices:
            print("  可选值：%s" % " / ".join(f.choices))
        elif f.kind in ("int", "float"):
            print("  范围  ：%s ~ %s" % (f.lo, f.hi))
        elif f.kind == "list":
            print("  格式  ：一项一个，用 、 或 , 分隔（例：赤珠山铁、小叶紫檀）")
        elif f.kind == "bool":
            print("  可选值：true / false")
        if f.managed:
            print("  ⚠ 这一项会被服务端下发覆盖（若已绑定后端）")
        cur = get_effective(f.path)
        src = "本地文件" if is_explicit(f.path) else "内置默认值"
        print("  当前值：%s   （来自 %s）" % (fmt(cur), src))
        print(THIN)
        print("  直接回车 = 不改   输入新值 = 保存   d = 恢复默认   q = 返回")

        if isinstance(cur, (list, tuple)):
            cur_str = "、".join(str(x) for x in cur)
        else:
            cur_str = "" if cur is None else fmt(cur)
        raw = ask("新值", cur_str)
        if raw is None:
            return
        if raw.lower() == "d":
            unset_value(f.path)
            input("  按回车继续…")
            return
        if raw == "":
            return
        ok, value, err, soft = coerce(f, raw)
        if not ok:
            if soft:
                print("  ! %s" % err)
                c = ask("确认仍要保存吗？(y/n)", "n")
                if not (c and parse_bool(c)):
                    continue
            else:
                print("  !! %s" % err)
                input("  按回车重试…")
                continue
        set_value(f.path, value)
        if f.path == "cloud.enabled":
            print()
            if value:
                print("  已启用托管。接下去要点「后端绑定」把地址和令牌填上并验证，")
                print("  否则仍会按独立运行处理。")
            else:
                print("  已切回独立运行：不连任何外部服务，报告只留在本机。")
        input("  按回车继续…")
        return


def menu_backend() -> None:
    while True:
        eff = effective().get("cloud") or {}
        mode, why = mode_summary()
        print()
        print(LINE)
        print("  后端绑定")
        print(LINE)
        print("  当前模式：%s  —— %s" % (mode, why))
        print("  服务器  ：%s" % (eff.get("base_url") or "（未设置）"))
        print("  令牌    ：%s" % (fmt(eff.get("token")) if eff.get("token") else "（未设置）"))
        print()
        print("  1) 绑定 / 更换服务器（填地址与令牌，当场验证，通过才写入）")
        print("  2) 验证当前绑定（只测试连通，不改配置）")
        print("  3) 解除绑定，回到独立运行")
        print("  0) 返回")
        c = ask("选择")
        if c is None or c == "0":
            return
        if c == "1":
            url = ask("服务器地址（如 https://stzb.example.com）", eff.get("base_url") or "")
            if url is None or not url:
                continue
            token = ask("采集端令牌（形如 stzb_xxxx）")
            if token is None or not token:
                continue
            print()
            print("· 正在验证 %s …" % url)
            ok, msg, detail = verify_backend(url, token)
            if not ok:
                print("  ✗ %s" % msg)
                print()
                print("  没有写入任何配置。可以先自查：")
                print("    · 浏览器能打开 %s/healthz 吗" % url)
                print("    · 令牌是不是整串复制全了")
                input("  按回车继续…")
                continue
            print("  ✓ 连接正常（服务端 %s %s，远端配置版本 v%s）"
                  % (detail.get("app"), detail.get("version"), detail.get("config_version")))
            set_value("cloud.base_url", url, quiet=True)
            set_value("cloud.token", token, quiet=True)
            set_value("cloud.enabled", True, quiet=True)
            print("  ✓ 已绑定并启用托管")
            input("  按回车继续…")
        elif c == "2":
            if not eff.get("base_url") or not eff.get("token"):
                print("  ! 还没绑定（缺地址或令牌）")
                input("  按回车继续…")
                continue
            ok, msg, detail = verify_backend(eff["base_url"], eff["token"])
            print(("  ✓ %s" % msg) if ok else ("  ✗ %s" % msg))
            if ok:
                print("      远端配置版本：v%s（可下发段：%s）"
                      % (detail.get("config_version"),
                         "、".join(detail.get("payload_sections") or []) or "还没配过"))
            input("  按回车继续…")
        elif c == "3":
            if not eff.get("enabled"):
                print("  ! 本来就是独立运行，无需解绑")
                input("  按回车继续…")
                continue
            c2 = ask("确认解除绑定并回到独立运行？(y/n)", "n")
            if c2 and parse_bool(c2):
                for p in ("cloud.base_url", "cloud.token"):
                    if is_explicit(p):
                        unset_value(p, quiet=True)
                set_value("cloud.enabled", False, quiet=True)
                print("  ✓ 已解除绑定。从现在起不连任何外部服务。")
            input("  按回车继续…")


def menu_group(section: str, title: str, fields: List[Field]) -> None:
    while True:
        print()
        print(LINE)
        print("  %s" % title)
        print(LINE)
        for i, f in enumerate(fields, 1):
            cur = get_effective(f.path)
            src = "" if is_explicit(f.path) else "  (默认)"
            warn = "  ⚠" if (f.managed and remote_override_note()) else ""
            print("  %2d) %s %s%s%s"
                  % (i, pad(f.label, 32), pad(fmt(cur), 26), src, warn))
        print()
        print("   0) 返回")
        c = ask("选择要改的项")
        if c is None or c == "0":
            return
        if c.isdigit() and 1 <= int(c) <= len(fields):
            menu_edit_field(fields[int(c) - 1])
        else:
            print("  ! 没有这个编号")


def menu_main() -> int:
    while True:
        mode, why = mode_summary()
        print()
        print(LINE)
        print("  率土之滨自动化 · 本机配置工具")
        print(LINE)
        print("  运行模式：%s  —— %s" % (mode, why))
        print("  配置文件：%s" % CONFIG_PATH)
        note = remote_override_note()
        if note:
            print()
            print("  " + note.replace("\n", "\n  "))
        print()
        print("  1) 后端托管        绑定 / 验证 / 解绑（不做就默认独立运行）")
        for i, (sec, title, _) in enumerate(GROUPS, start=2):
            if sec == "cloud":
                continue
            print("  %2d) %s" % (i, title))
        print("  %2d) 查看全部配置（标注来源与是否被服务端接管）" % (len(GROUPS) + 1))
        print("  %2d) 备份与恢复" % (len(GROUPS) + 2))
        print("   0) 退出")
        c = ask("选择")
        if c is None or c == "0":
            print()
            print("配置已保存在 %s" % CONFIG_PATH)
            print("改完想确认：python run_daily.py --status")
            return 0
        if c == "1":
            menu_backend()
            continue
        secs = [g for g in GROUPS if g[0] != "cloud"]
        if c.isdigit():
            n = int(c)
            if 2 <= n < 2 + len(secs):
                sec, title, fields = secs[n - 2]
                menu_group(sec, title, fields)
                continue
            if n == len(GROUPS) + 1:
                print()
                for gsec, gtitle, gfields in GROUPS:
                    print("%s" % gtitle)
                    for f in gfields:
                        tag = "  ← 服务端可覆盖" if f.managed else ""
                        print("  %s %s %s%s"
                              % (pad(f.path, 34), pad(fmt(get_effective(f.path)), 40),
                                 "本地" if is_explicit(f.path) else "默认", tag))
                    print()
                input("  按回车继续…")
                continue
            if n == len(GROUPS) + 2:
                menu_backup()
                continue
        print("  ! 没有这个编号")


def menu_backup() -> None:
    while True:
        print()
        print(LINE)
        print("  备份与恢复")
        print(LINE)
        items = list_backups()
        print("  备份目录：%s（保留最近 %d 份）" % (BACKUP_DIR, KEEP_BACKUPS))
        print("  现有备份：%d 份" % len(items))
        if items:
            print("  最新一份：%s" % items[0])
        print()
        print("  1) 立即备份当前配置")
        print("  2) 从备份恢复")
        print("  0) 返回")
        c = ask("选择")
        if c is None or c == "0":
            return
        if c == "1":
            p = backup()
            print("  ✓ 已备份到 %s" % p if p else "  ! 没有 config.json 可备份")
            input("  按回车继续…")
        elif c == "2":
            if not items:
                print("  ! 还没有备份")
                input("  按回车继续…")
                continue
            for i, f in enumerate(items[:10], 1):
                print("  %2d) %s" % (i, f))
            c2 = ask("要恢复哪一份（编号）")
            if c2 and c2.isdigit() and 1 <= int(c2) <= min(len(items), 10):
                name = items[int(c2) - 1]
                backup()
                try:
                    with open(os.path.join(BACKUP_DIR, name), encoding="utf-8") as f:
                        save_raw(json.load(f))
                    print("  ✓ 已从 %s 恢复（恢复前的也备份了）" % name)
                except Exception as e:
                    print("  ! 恢复失败：%r" % (e,))
            input("  按回车继续…")


def cmd_menu(args) -> int:
    if not sys.stdin.isatty():
        print("!! 当前不是交互终端（可能是被重定向 / 管道调用了）。")
        print("   请直接双击 config_tool.bat，或改用命令行子命令：")
        print("     python tools/config.py show")
        print("     python tools/config.py bind --url https://xx --token stzb_xx")
        print("     python tools/config.py set recruit.half_price false")
        return 2
    try:
        return menu_main()
    except (KeyboardInterrupt, EOFError):
        print()
        print("已取消。配置已保存在 %s" % CONFIG_PATH)
        return 0


# --------------------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="config.py",
        description="本机配置工具（可选工具，不用它也不影响脚本正常运行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python tools/config.py                        交互式菜单
  python tools/config.py show                   看全部配置
  python tools/config.py show --section cloud   只看后端那一段
  python tools/config.py set recruit.half_price false
  python tools/config.py bind --url https://stzb.example.com --token stzb_xxx
  python tools/config.py unbind                 回到独立运行
  python tools/config.py verify                 只验证连通
  python tools/config.py backup / restore        备份与恢复
""")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("menu", help="交互式菜单（默认）")
    p.set_defaults(fn=cmd_menu)

    p = sub.add_parser("show", help="打印当前配置")
    p.add_argument("--section", default=None, help="只看某一段，如 cloud / device / recruit")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("get", help="取某一项的值")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("set", help="设置某一项")
    p.add_argument("path")
    p.add_argument("value")
    p.add_argument("--force", action="store_true",
                   help="跳过「看起来填错了」的检查（如路径不存在），格式错误仍会拒绝")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("reset", help="把某一项恢复出厂默认（或 all 清空整个文件）")
    p.add_argument("path")
    p.set_defaults(fn=cmd_reset)

    p = sub.add_parser("bind", help="绑定后端并当场验证")
    p.add_argument("--url", default="")
    p.add_argument("--token", default="")
    p.add_argument("--timeout", type=int, default=20)
    p.set_defaults(fn=cmd_bind)

    p = sub.add_parser("unbind", help="解除绑定，回到独立运行")
    p.set_defaults(fn=cmd_unbind)

    p = sub.add_parser("verify", help="只验证后端连通性")
    p.add_argument("--url", default="")
    p.add_argument("--token", default="")
    p.add_argument("--timeout", type=int, default=20)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("backup", help="备份 config.json")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("restore", help="从备份恢复")
    p.add_argument("--name", default="", help="备份文件名或编号；不填则列出")
    p.set_defaults(fn=cmd_restore)

    args = ap.parse_args()
    if not getattr(args, "fn", None):
        args.fn = cmd_menu
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
