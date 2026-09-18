# -*- coding: utf-8 -*-
"""一轮执行的结果报告。

产出三份东西：
  logs/reports/run_<时间戳>.html   —— 自包含单文件报告，截图以 base64 内嵌，双击就能看
  logs/reports/run_<时间戳>.json   —— 结构化结果，给自动化 / 程序读
  logs/reports/latest.json|md      —— 永远指向最近一次，方便外部脚本和定时播报

为什么截图要内嵌 base64：
  原图单张 3MB 左右，一轮跑下来上百张。报告如果引用相对路径，用户把 html 单独拷走就全裂了；
  如果原图内嵌，html 会有几百 MB 打不开。所以统一缩到宽 ≤900px、JPEG q72 再内嵌，
  单张约 60~120KB，整份报告 1~3MB，可接受。
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

import cv2
import numpy as np

# 截图文件名末尾的全局序号：`sj_panel_01.png` -> 1、`tx_panel_3_106.png` -> 106
_SHOT_SEQ_RE = re.compile(r"_(\d+)(?:\.[A-Za-z0-9]+)*$")

# 报告里每个任务最多内嵌几张截图（挑法见 pick_shots）
MAX_SHOTS_PER_TASK = 5
THUMB_WIDTH = 900
THUMB_QUALITY = 72

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_ERROR = "error"

_STATUS_TEXT = {
    STATUS_OK: ("完成", "#0a7f3f", "#e6f6ec"),
    STATUS_FAIL: ("失败", "#b3261e", "#fdecea"),
    STATUS_SKIP: ("跳过", "#8a6d00", "#fff6da"),
    STATUS_ERROR: ("异常", "#b3261e", "#fdecea"),
}


def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def _read_image(path: str) -> Optional[np.ndarray]:
    """读图。cv2.imread 在中文路径上会静默失败，必须走 imdecode。"""
    try:
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def _embed_jpg(path: str, width: int = THUMB_WIDTH) -> Optional[str]:
    """把一张截图缩到 width 宽、编码成 JPEG 再 base64。失败返回 None。"""
    img = _read_image(path)
    if img is None:
        return None
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), THUMB_QUALITY])
    if not ok:
        return None
    return base64.b64encode(buf.tobytes()).decode("ascii")


def pick_shots(shots: Sequence[str], limit: int = MAX_SHOTS_PER_TASK) -> List[str]:
    """从任务的一堆截图里挑最有信息量的几张。

    优先带 panel 的（面板全貌），再补每个任务的最后一张（最终状态），
    数量还不够就按等间隔补。保持原有先后顺序，报告读起来才是时间序。
    """
    shots = list(shots)
    if len(shots) <= limit:
        return shots
    chosen: List[str] = []

    def add(p):
        if p not in chosen:
            chosen.append(p)

    for p in shots:
        if "panel" in os.path.basename(p).lower():
            add(p)
    add(shots[-1])
    if len(chosen) < limit:
        step = max(1, len(shots) // (limit - len(chosen) + 1))
        for i in range(0, len(shots), step):
            if len(chosen) >= limit:
                break
            add(shots[i])
    order = {p: i for i, p in enumerate(shots)}
    return sorted(chosen[:limit], key=lambda p: order.get(p, 0))


@dataclass
class TaskEntry:
    key: str
    name: str
    status: str = STATUS_SKIP
    seconds: float = 0.0
    reason: str = ""
    notes: List[str] = field(default_factory=list)
    shots: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


class TaskRecorder:
    """交给任务用的 logger：既转发到主日志，又记进报告。"""

    def __init__(self, entry: TaskEntry, sink: Callable[[str], None]):
        self.entry = entry
        self._sink = sink

    def __call__(self, msg: str):
        self.entry.notes.append(msg)
        self._sink(msg)

    def info(self, msg: str):
        """只进报告、不刷屏的附加说明。"""
        self.entry.notes.append(msg)


class RunReport:
    """一轮执行的全部结果。"""

    def __init__(self, root: str, shot_dir: str, slot: str = "",
                 dry_run: bool = False, logger: Optional[Callable[[str], None]] = None):
        self.root = root
        self.shot_dir = shot_dir
        self.slot = slot
        self.dry_run = dry_run
        self._log = logger or (lambda m: None)
        self.started_at = dt.datetime.now()
        self.finished_at: Optional[dt.datetime] = None
        self.entries: List[TaskEntry] = []
        self.notes: List[str] = []
        self.env: Dict[str, str] = {}
        self._before: set = set()

    # ------------------------------------------------------------ 运行级信息

    def note(self, msg: str):
        self.notes.append(msg)

    def env_info(self, **kw):
        for k, v in kw.items():
            if v is not None:
                self.env[k] = str(v)

    # ------------------------------------------------------------ 任务

    def _shot_files(self) -> set:
        try:
            return {f for f in os.listdir(self.shot_dir) if f.endswith(".png")}
        except Exception:
            return set()

    def skip(self, key: str, name: str, reason: str):
        e = TaskEntry(key=key, name=name, status=STATUS_SKIP, reason=reason)
        self.entries.append(e)
        return e

    def begin(self, key: str, name: str) -> TaskRecorder:
        e = TaskEntry(key=key, name=name, status=STATUS_FAIL)
        self.entries.append(e)
        self._before = self._shot_files()
        return TaskRecorder(e, self._log)

    def finish(self, rec: TaskRecorder, ok: bool, status: Optional[str] = None,
               reason: str = "", seconds: float = 0.0):
        e = rec.entry
        e.status = status or (STATUS_OK if ok else STATUS_FAIL)
        e.reason = reason
        e.seconds = seconds
        new = self._shot_files() - self._before
        # OCR 放大重试产生的中间图（xxx.png.x2.png）不进报告，太吵
        new = {f for f in new if ".x2." not in f}
        e.shots = self._by_seq(new)

    def _by_seq(self, files) -> List[str]:
        """按「截图序号」排序 = 拍摄先后。

        文件名末尾那个数字来自 Device 的**全局自增计数器**（shot_path 里 `%03d`），
        所以它天然就是拍摄顺序，比 mtime 可靠得多。

        为什么不用 mtime：同一个系统时钟刻度内写的两个文件 mtime 会**完全相同**
        （Windows 的时钟粒度约 15ms，实测能撞到），那时排序只能退回按文件名，
        而跨 tag 的字母序和时间序毫无关系 —— `sj_ok_02` 会排到 `sj_panel_01` 前面，
        序号过 100 后 `_100` 还小于 `_099`。这两个坑都真踩过。
        """
        def key(name: str):
            m = _SHOT_SEQ_RE.search(name)
            return (int(m.group(1)) if m else 10 ** 9, name)

        return [os.path.join(self.shot_dir, f) for f in sorted(files, key=key)]

    # ------------------------------------------------------------ 统计

    @property
    def n_ok(self) -> int:
        return sum(1 for e in self.entries if e.status == STATUS_OK)

    @property
    def n_fail(self) -> int:
        return sum(1 for e in self.entries if e.status in (STATUS_FAIL, STATUS_ERROR))

    @property
    def n_skip(self) -> int:
        return sum(1 for e in self.entries if e.status == STATUS_SKIP)

    @property
    def all_ok(self) -> bool:
        return self.n_fail == 0

    @property
    def duration(self) -> float:
        end = self.finished_at or dt.datetime.now()
        return (end - self.started_at).total_seconds()

    def summary_text(self) -> str:
        lines = ["%s  档位 %s%s  耗时 %.1fs"
                 % (self.started_at.strftime("%Y-%m-%d %H:%M"),
                    self.slot or "-",
                    "（预演）" if self.dry_run else "",
                    self.duration)]
        lines.append("成功 %d / 失败 %d / 跳过 %d" % (self.n_ok, self.n_fail, self.n_skip))
        for e in self.entries:
            mark = _STATUS_TEXT[e.status][0]
            extra = ("  —— " + e.reason) if e.reason else ""
            lines.append("  [%s] %-22s %5.1fs  截图%d张%s"
                         % (mark, e.name, e.seconds, len(e.shots), extra))
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": (self.finished_at or dt.datetime.now()).isoformat(timespec="seconds"),
            "slot": self.slot,
            "dry_run": self.dry_run,
            "duration_seconds": round(self.duration, 1),
            "counts": {"ok": self.n_ok, "fail": self.n_fail, "skip": self.n_skip},
            "all_ok": self.all_ok,
            "env": self.env,
            "notes": self.notes,
            "tasks": [{
                "key": e.key,
                "name": e.name,
                "status": e.status,
                "seconds": round(e.seconds, 1),
                "reason": e.reason,
                "notes": e.notes,
                "shots": e.shots,
            } for e in self.entries],
        }

    # ------------------------------------------------------------ 落盘

    def write_json(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def render_html(self, path: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._html())
        return path

    # ------------------------------------------------------------ HTML

    def _html(self) -> str:
        esc = html.escape
        banner_bg = "#e6f6ec" if self.all_ok else "#fdecea"
        banner_fg = "#0a7f3f" if self.all_ok else "#b3261e"
        headline = ("全部完成" if self.all_ok
                    else "%d 项未完成" % self.n_fail)

        rows = []
        for e in self.entries:
            label, fg, bg = _STATUS_TEXT[e.status]
            rows.append(
                '<tr><td class="tname"><a href="#t_%s">%s</a></td>'
                '<td><span class="pill" style="color:%s;background:%s">%s</span></td>'
                '<td class="num">%.1fs</td><td class="num">%d</td>'
                '<td class="reason">%s</td></tr>'
                % (esc(e.key), esc(e.name), fg, bg, label, e.seconds, len(e.shots),
                   esc(e.reason) or "&nbsp;"))

        sections = []
        for e in self.entries:
            label, fg, bg = _STATUS_TEXT[e.status]
            notes = "".join('<div class="ln">%s</div>' % esc(n) for n in e.notes) \
                or '<div class="ln dim">（无日志）</div>'
            shots = pick_shots(e.shots)
            thumbs = []
            for p in shots:
                b64 = _embed_jpg(p)
                if not b64:
                    continue
                thumbs.append(
                    '<figure><img src="data:image/jpeg;base64,%s" alt="%s">'
                    '<figcaption>%s</figcaption></figure>' % (b64, esc(os.path.basename(p)),
                                                              esc(os.path.basename(p))))
            skipped = len(e.shots) - len(shots)
            shot_note = ""
            if e.shots:
                shot_note = ('<div class="dim">共 %d 张截图，内嵌 %d 张%s</div>'
                             % (len(e.shots), len(thumbs),
                                ("，其余在 %s" % esc(self.shot_dir)) if skipped > 0 else ""))
            sections.append("""
<section class="task" id="t_%(key)s">
  <h2>%(name)s <span class="pill" style="color:%(fg)s;background:%(bg)s">%(label)s</span>
      <span class="dim h2meta">%(secs).1fs · %(nshot)d 张截图</span></h2>
  %(reason)s
  <details %(open)s><summary>执行日志（%(nlines)d 行）</summary><div class="logbox">%(notes)s</div></details>
  %(shotnote)s
  <div class="shots">%(thumbs)s</div>
</section>""" % {
                "key": esc(e.key), "name": esc(e.name), "fg": fg, "bg": bg, "label": label,
                "secs": e.seconds, "nshot": len(e.shots),
                "reason": ('<div class="reason2">%s</div>' % esc(e.reason)) if e.reason else "",
                "open": "open" if e.status != STATUS_OK else "",
                "nlines": len(e.notes), "notes": notes, "shotnote": shot_note,
                "thumbs": "".join(thumbs),
            })

        env_rows = "".join('<tr><th>%s</th><td>%s</td></tr>' % (esc(k), esc(v))
                           for k, v in self.env.items())
        run_notes = "".join('<div class="ln">%s</div>' % esc(n) for n in self.notes) \
            or '<div class="ln dim">（无）</div>'

        return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>率土每日任务报告 %(stamp)s</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f5f6f8;color:#1c1e21;
     font:15px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px}
.banner{background:%(bbg)s;color:%(bfg)s;border-radius:14px;padding:22px 26px;margin-bottom:20px}
.banner h1{margin:0 0 6px;font-size:24px}
.banner .sub{opacity:.85;font-size:14px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}
.card{flex:1 1 150px;background:#fff;border-radius:12px;padding:14px 16px;
      box-shadow:0 1px 3px rgba(0,0,0,.07)}
.card .k{font-size:12px;color:#6b7280}
.card .v{font-size:22px;font-weight:600;margin-top:2px}
table{width:100%%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;
      box-shadow:0 1px 3px rgba(0,0,0,.07);margin-bottom:24px}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #eef0f2;font-size:14px}
thead th{background:#fafbfc;font-size:12px;color:#6b7280;font-weight:600}
tbody tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.reason{color:#6b7280;font-size:13px}
.tname a{color:#1c1e21;text-decoration:none;font-weight:600}
.tname a:hover{color:#1668c1}
.pill{display:inline-block;padding:1px 10px;border-radius:999px;font-size:12px;font-weight:600}
.task{background:#fff;border-radius:12px;padding:18px 20px;margin-bottom:16px;
      box-shadow:0 1px 3px rgba(0,0,0,.07)}
.task h2{margin:0 0 10px;font-size:17px}
.h2meta{font-weight:400;font-size:13px;margin-left:6px}
.dim{color:#6b7280}
.reason2{background:#fafbfc;border-left:3px solid #d1d5db;padding:8px 12px;
         border-radius:6px;margin-bottom:10px;font-size:13px;color:#374151}
details{margin:10px 0}
summary{cursor:pointer;color:#1668c1;font-size:13px}
.logbox{margin-top:8px;background:#fafbfc;border:1px solid #eef0f2;border-radius:8px;
        padding:10px 12px;max-height:320px;overflow:auto}
.ln{font:12px/1.7 ui-monospace,Consolas,monospace;white-space:pre-wrap;
    word-break:break-all;color:#374151}
.shots{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}
figure{margin:0;width:calc(50%% - 6px)}
figure img{width:100%%;border-radius:8px;border:1px solid #e5e7eb;display:block}
figcaption{font-size:11px;color:#9ca3af;margin-top:4px;
           overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.envth{width:180px;color:#6b7280;font-weight:600}
h3.sec{font-size:15px;margin:26px 0 10px}
@media(max-width:700px){figure{width:100%%}}
</style></head><body><div class="wrap">
<div class="banner"><h1>%(headline)s</h1>
  <div class="sub">%(stamp)s · 档位 %(slot)s%(dry)s · 总耗时 %(dur).1f 秒</div></div>
<div class="cards">
  <div class="card"><div class="k">完成</div><div class="v" style="color:#0a7f3f">%(nok)d</div></div>
  <div class="card"><div class="k">失败 / 异常</div><div class="v" style="color:#b3261e">%(nfail)d</div></div>
  <div class="card"><div class="k">跳过</div><div class="v" style="color:#8a6d00">%(nskip)d</div></div>
  <div class="card"><div class="k">截图总数</div><div class="v">%(nshots)d</div></div>
</div>
<table><thead><tr><th>任务</th><th>结果</th><th class="num">耗时</th>
<th class="num">截图</th><th>说明</th></tr></thead><tbody>
%(rows)s
</tbody></table>
<h3 class="sec">运行环境</h3>
<table><tbody>%(envrows)s</tbody></table>
<h3 class="sec">启动过程</h3>
<section class="task"><div class="logbox">%(runnotes)s</div></section>
<h3 class="sec">任务详情</h3>
%(sections)s
</div></body></html>""" % {
            "stamp": esc(self.started_at.strftime("%Y-%m-%d %H:%M:%S")),
            "slot": esc(self.slot or "-"),
            "dry": "（预演，不点击）" if self.dry_run else "",
            "dur": self.duration,
            "headline": esc(headline), "bbg": banner_bg, "bfg": banner_fg,
            "nok": self.n_ok, "nfail": self.n_fail, "nskip": self.n_skip,
            "nshots": sum(len(e.shots) for e in self.entries),
            "rows": "".join(rows) or '<tr><td colspan="5" class="dim">没有任务被执行</td></tr>',
            "envrows": env_rows or '<tr><td class="dim">（无）</td></tr>',
            "runnotes": run_notes,
            "sections": "".join(sections) or '<section class="task dim">没有任务被执行</section>',
        }

    def write_all(self, report_dir: str, keep_days: int = 14) -> Dict[str, str]:
        self.finished_at = self.finished_at or dt.datetime.now()
        os.makedirs(report_dir, exist_ok=True)
        stamp = self.started_at.strftime("%Y-%m-%d_%H%M%S")
        html_path = os.path.join(report_dir, "run_%s.html" % stamp)
        json_path = os.path.join(report_dir, "run_%s.json" % stamp)
        self.render_html(html_path)
        self.write_json(json_path)
        latest_json = os.path.join(report_dir, "latest.json")
        latest_md = os.path.join(report_dir, "latest.md")
        latest_html = os.path.join(report_dir, "latest.html")
        self.write_json(latest_json)
        with open(latest_md, "w", encoding="utf-8") as f:
            f.write("# 率土每日任务 —— 最近一次执行\n\n```\n%s\n```\n" % self.summary_text())
        # latest.html 用拷贝而不是重新渲染：报告里嵌了几十张图，重渲染要多花好几秒。
        # 它不叫 run_*，所以下面的清理逻辑不会碰它。
        try:
            shutil.copyfile(html_path, latest_html)
        except Exception:
            latest_html = ""
        self._prune(report_dir, keep_days)
        return {"html": html_path, "json": json_path,
                "latest_json": latest_json, "latest_md": latest_md,
                "latest_html": latest_html}

    @staticmethod
    def _prune(report_dir: str, keep_days: int):
        """删掉过期的 run_* 报告。只删本目录里自己生成的 run_ 前缀文件。"""
        if keep_days <= 0:
            return
        cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
        for name in os.listdir(report_dir):
            if not name.startswith("run_"):
                continue
            p = os.path.join(report_dir, name)
            try:
                if dt.datetime.fromtimestamp(os.path.getmtime(p)) < cutoff:
                    os.remove(p)
            except Exception:
                pass
