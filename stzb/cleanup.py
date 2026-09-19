# -*- coding: utf-8 -*-
"""日志 / 截图 / 报告的定期清理。

**为什么要有这个模块（2026-09-19 实测）：**
截图单张 2~3.7MB，跑两天就堆到 **5.5GB / 2576 张**。原来代码里只有报告目录
有个「删过期 run_* 」的小逻辑，**截图目录从来没人清理** —— 这才是磁盘杀手。

**设计原则（每一条都是防事故的）：**
1. **只删自己认识的文件**（白名单式）：shots 只删图片后缀；reports 只删 `run_` 前缀
   的文件；文本日志只删 `run_*.log`。绝不 `rmtree`、绝不用通配符删任意文件。
2. **绝不跟随符号链接**：`islink()` 一律跳过，防止顺着软链把目录外的东西删掉。
3. **只删文件，不删目录**：任何 `isdir()` 一律跳过（`diag/` 里有子目录和人工脚本）。
4. **`latest.*` / `console.log` 永不删**：那是「看最近一次」的快捷入口。
5. **体积上限清理只从「非当天」文件里删**：保证当天刚拍的截图永远安全，
   哪怕当天一轮就跑超了上限。
6. `keep_days <= 0` = 不按天数清理；`max_mb <= 0` = 不限体积。

清理是**幂等**的：可以随时重复跑，不会越删越多，也不会删错。
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Dict, List, Optional, Tuple

# 截图目录里允许被清理的图片后缀（白名单）
IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
# 报告目录里允许被清理的文件前缀
REPORT_PREFIX = "run_"
# 这些文件名永不删（不论多老）
KEEP_NAMES = {
    "latest.html", "latest.json", "latest.md", "console.log",
}
# 诊断目录里的图片可以按天清，但绝不碰 .py / 子目录（那是人工侦察产物）
DIAG_EXT = IMG_EXT


def _day_start_epoch() -> float:
    """今天 0 点的 epoch 秒。用于「当天文件绝不动」这条铁律。"""
    d = dt.date.today()
    return dt.datetime(d.year, d.month, d.day).timestamp()


def _scan(folder: str) -> List[Tuple[str, str, float, int]]:
    """列出一级目录下的**普通文件**。

    返回 [(full_path, name, mtime, size)]。
    跳过：目录、符号链接、读不到 stat 的项（都当成「不是我们的东西」）。
    只扫一级 —— 已知结构都是平铺的，递归反而危险。
    """
    out: List[Tuple[str, str, float, int]] = []
    try:
        names = os.listdir(folder)
    except Exception:
        return out
    for name in names:
        p = os.path.join(folder, name)
        try:
            if os.path.islink(p):          # 符号链接一律不碰
                continue
            if not os.path.isfile(p):      # 目录一律不碰
                continue
            st = os.stat(p)
        except Exception:
            continue
        out.append((p, name, st.st_mtime, st.st_size))
    return out


def _human(n: float) -> str:
    """把字节数变成人能读的串。"""
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % int(n)


def usage(folder: str, exts: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
    """统计一个目录的体积（只算普通文件）。用于日志与报告展示。"""
    files = _scan(folder)
    if exts is not None:
        files = [f for f in files if f[1].lower().endswith(exts)]
    total = sum(f[3] for f in files)
    return {"count": len(files), "bytes": total, "human": _human(total)}


def _prune_folder(folder: str, *, exts: Optional[Tuple[str, ...]],
                  prefix: Optional[str], keep_days: int, max_mb: float,
                  protect_today_for_size: bool = True,
                  label: str = "", dry_run: bool = False) -> Dict[str, Any]:
    """清理一个目录。返回 {deleted, freed, freed_human, kept, kept_bytes, errors}。

    · exts    —— 只清理这些后缀（None = 不限后缀）
    · prefix  —— 只清理这个前缀的文件（None = 不限前缀）
    · keep_days —— 删掉 mtime 早于「now - keep_days 天」的；<=0 则不按天清
    · max_mb  —— 体积上限；<=0 则不按体积清
    · dry_run —— 只算不删（给人先看清会删什么）。此时 `deleted` 是「将会删的个数」。
    """
    res: Dict[str, Any] = {"label": label, "deleted": 0, "freed": 0,
                           "freed_human": "0 B", "kept": 0, "kept_bytes": 0,
                           "errors": 0, "scanned": 0, "dry_run": dry_run}

    def _mine(name: str) -> bool:
        """这个文件是不是「我们生成的、可以清理的」。"""
        if name in KEEP_NAMES:                      # 快捷入口永不删
            return False
        low = name.lower()
        if name.startswith("_"):                    # 侦察留下的对比图，不碰
            return False
        if exts is not None and not low.endswith(exts):
            return False
        if prefix is not None and not name.startswith(prefix):
            return False
        return True

    files = [f for f in _scan(folder) if _mine(f[1])]
    res["scanned"] = len(files)
    if not files:
        return res

    doomed: List[Tuple[str, str, float, int]] = []
    doomed_paths: set = set()

    # ---- 第一轮：按天清理 ----
    if keep_days > 0:
        cutoff_ts = (dt.datetime.now() - dt.timedelta(days=keep_days)).timestamp()
        for f in files:
            if f[2] < cutoff_ts:
                doomed.append(f)
                doomed_paths.add(f[0])

    # ---- 第二轮：按体积清理（只从「非当天」里挑，当天永不动） ----
    if max_mb > 0:
        limit = max_mb * (1 << 20)
        remaining = [f for f in files if f[0] not in doomed_paths]
        total = sum(f[3] for f in remaining)
        if total > limit:
            pool_ts = _day_start_epoch() if protect_today_for_size else float("inf")
            # 候选 = 非当天的，从最老开始删，删到低于上限为止
            old_first = sorted((f for f in remaining if f[2] < pool_ts),
                               key=lambda x: x[2])
            for f in old_first:
                if total <= limit:
                    break
                doomed.append(f)
                doomed_paths.add(f[0])
                total -= f[3]

    # ---- 执行删除 ----
    for path, name, mtime, size in doomed:
        if dry_run:                     # 预览：只统计，不真删
            res["deleted"] += 1
            res["freed"] += size
            continue
        try:
            os.remove(path)
            res["deleted"] += 1
            res["freed"] += size
        except Exception:
            res["errors"] += 1

    kept = [f for f in files if f[0] not in doomed_paths]
    res["kept"] = len(kept)
    res["kept_bytes"] = sum(f[3] for f in kept)
    res["freed_human"] = _human(res["freed"])
    return res


def cleanup_by_days(root: str, keep_days: int = 14,
                    shots_keep_days: Optional[int] = None,
                    shots_max_mb: float = 0,
                    log_keep_days: Optional[int] = None,
                    diag: bool = True,
                    logger=None,
                    dry_run: bool = False) -> Dict[str, Any]:
    """清理 `logs/` 下的截图 / 报告 / 文本日志（以及可选的 diag 图片）。

    · keep_days       —— 报告与文本日志的保留天数（0 = 不清理）
    · shots_keep_days —— 截图的保留天数；None 表示跟随 keep_days
                         （截图最占空间，通常设得比报告短）
    · shots_max_mb    —— 截图目录体积上限（MB），0 = 不限
    · log_keep_days   —— 文本日志保留天数；None 表示跟随 keep_days
    · diag            —— 是否顺带清理 diag/ 里的过期图片（.py 和子目录绝不动）

    返回汇总：{deleted, freed, freed_human, areas:[...]}，顺手调 logger 打印。
    """
    log = logger or (lambda m: None)
    shot_days = keep_days if shots_keep_days is None else shots_keep_days
    text_days = keep_days if log_keep_days is None else log_keep_days

    logs_dir = os.path.join(root, "logs")
    areas: List[Dict[str, Any]] = []

    # ① 截图：最占空间，可单独设更短的天数 + 体积兜底
    areas.append(_prune_folder(
        os.path.join(logs_dir, "shots"),
        exts=IMG_EXT, prefix=None,
        keep_days=shot_days, max_mb=shots_max_mb,
        protect_today_for_size=True, label="截图", dry_run=dry_run))

    # ② 执行报告：只删 run_* 前缀，latest.* 受 KEEP_NAMES 保护
    areas.append(_prune_folder(
        os.path.join(logs_dir, "reports"),
        exts=None, prefix=REPORT_PREFIX,
        keep_days=keep_days, max_mb=0, label="报告", dry_run=dry_run))

    # ③ 文本日志：只删 run_YYYY-MM-DD.log
    areas.append(_prune_folder(
        logs_dir, exts=(".log",), prefix=REPORT_PREFIX,
        keep_days=text_days, max_mb=0, label="文本日志", dry_run=dry_run))

    # ④ 诊断图片：可选。绝不碰 .py / 子目录（人工侦察证据）
    if diag:
        areas.append(_prune_folder(
            os.path.join(logs_dir, "diag"),
            exts=DIAG_EXT, prefix=None,
            keep_days=shot_days, max_mb=0, label="诊断图片", dry_run=dry_run))

    total_del = sum(a["deleted"] for a in areas)
    total_free = sum(a["freed"] for a in areas)
    result = {"deleted": total_del, "freed": total_free,
              "freed_human": _human(total_free), "areas": areas, "dry_run": dry_run}

    verb = "将清理" if dry_run else "清理"
    if total_del:
        log("· %s过期文件：%d 个，%s %s"
            % (verb, total_del, "预计释放" if dry_run else "释放", result["freed_human"]))
        for a in areas:
            if a["deleted"]:
                log("    %-6s %s %d 个，释放 %s（保留 %d 个 / %s）"
                    % (a["label"], "将删" if dry_run else "删", a["deleted"],
                       a["freed_human"], a["kept"], _human(a["kept_bytes"])))
    else:
        log("· 清理过期文件：没有需要清理的")
    return result


def summary_line(root: str) -> str:
    """一行说明当前占用，用于状态/日志展示。"""
    logs_dir = os.path.join(root, "logs")
    sh = usage(os.path.join(logs_dir, "shots"), IMG_EXT)
    rp = usage(os.path.join(logs_dir, "reports"))
    return "截图 %d 张 / %s；报告 %d 个 / %s" % (
        sh["count"], sh["human"], rp["count"], rp["human"])


def parse_cfg(cfg, root: str) -> Dict[str, Any]:
    """从配置对象里取出清理参数（带默认值，缺项不会崩）。

    `logging.shots_keep_days` / `logging.log_keep_days` 用 -1 表示「跟随 keep_days」，
    因为 JSON 里写 null 不如写 -1 直观，且不会被误当成 0（0 = 不清理，语义差很远）。
    """
    get = getattr(cfg, "get", None)
    if get is None:
        return {"keep_days": 14, "shots_keep_days": None, "shots_max_mb": 0,
                "log_keep_days": None, "diag": True}

    def _int(path, default):
        try:
            return int(get(path, default))
        except Exception:
            return default

    def _follow(path, default):
        """-1 或缺失 = 跟随 keep_days（返回 None）。"""
        v = _int(path, -1)
        return None if v < 0 else v

    return {
        "keep_days": _int("logging.keep_days", 14),
        "shots_keep_days": _follow("logging.shots_keep_days", -1),
        "shots_max_mb": _int("logging.shots_max_mb", 0),
        "log_keep_days": _follow("logging.log_keep_days", -1),
        "diag": bool(get("logging.cleanup_diag", True)),
    }