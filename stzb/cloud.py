# -*- coding: utf-8 -*-
"""把运行结果、报告和截图上传到后端控制台；并从后端拉配置与待执行任务。

**只用标准库**（urllib + ssl + json），不引入 requests/httpx。
这样脚本换台机器、换个 Python 环境都能跑，不会因为少个包当场挂掉。

TLS 说明：优先用 certifi 的证书包（包含 Let's Encrypt 的 ISRG Root X1/X2），
拿不到就退回系统默认信任库。两种都失败时会明确报错，不会静默降级成不校验 ——
往公网传数据，绝不能悄悄跳过证书校验。
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import platform
import secrets
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

RUNNER_VERSION = "1.1.0"

# 单次请求超时（秒）。报告 3MB 左右，60 秒够；上传成功与否不该拖垮整轮任务
DEFAULT_TIMEOUT = 90
DEFAULT_RETRIES = 3

# 心跳短超时：心跳是高频小请求，卡 90 秒没意义，也不该阻塞心跳线程
HEARTBEAT_TIMEOUT = 12


class CloudError(RuntimeError):
    pass


def hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return platform.node() or "unknown"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi                     # 有就用它，根证书最全
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception as e:                  # pragma: no cover - 极端环境
        raise CloudError("无法建立 TLS 信任库：%r" % (e,))


def encode_multipart(fields: Dict[str, str],
                     files: Sequence[Tuple[str, str, bytes, str]]
                     ) -> Tuple[bytes, str]:
    """手搓 multipart/form-data。

    files 里每项是 (字段名, 文件名, 内容, MIME)。
    返回 (body, content_type)。
    """
    boundary = "----stzb" + secrets.token_hex(16)
    buf = bytearray()
    for k, v in (fields or {}).items():
        buf += ("--%s\r\n" % boundary).encode("utf-8")
        buf += ('Content-Disposition: form-data; name="%s"\r\n\r\n'
                % str(k)).encode("utf-8")
        buf += str(v).encode("utf-8")
        buf += b"\r\n"
    for name, filename, content, ctype in files:
        buf += ("--%s\r\n" % boundary).encode("utf-8")
        buf += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (name, filename)).encode("utf-8")
        buf += ("Content-Type: %s\r\n\r\n" % (ctype or "application/octet-stream")).encode("utf-8")
        buf += content
        buf += b"\r\n"
    buf += ("--%s--\r\n" % boundary).encode("utf-8")
    return bytes(buf), "multipart/form-data; boundary=%s" % boundary


@dataclass
class CloudClient:
    """后端客户端。base_url 形如 https://stzb.example.com（末尾斜杠可有可无）。"""

    base_url: str
    token: str
    timeout: int = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    logger: Callable[[str], None] = print
    ctx: Optional[ssl.SSLContext] = None
    uid: str = ""                     # 客户端稳定标识，随每个请求头发上去
    stats: Dict[str, int] = field(default_factory=lambda: {"up": 0, "down": 0, "fail": 0})

    def __post_init__(self):
        self.base_url = (self.base_url or "").rstrip("/")
        if not self.base_url:
            raise CloudError("cloud.base_url 没填")
        if not self.token:
            raise CloudError("cloud.token 没填")
        if self.ctx is None:
            self.ctx = _ssl_context()

    # -------------------------------------------------------------- 底层

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        u = self.base_url + path
        if params:
            u += "?" + urllib.parse.urlencode(params)
        return u

    def request(self, method: str, path: str, *, body: Optional[bytes] = None,
                content_type: str = "application/json",
                params: Optional[Dict[str, Any]] = None,
                want_json: bool = True, timeout: Optional[int] = None,
                retries: Optional[int] = None
                ) -> Tuple[bool, Any]:
        """返回 (成功?, 解析后的 JSON 或错误文本)。网络类错误会重试。

        retries 默认用实例级配置；高频小请求（心跳）会显式传 1，重试反而拖慢下一拍。
        """
        url = self._url(path, params)
        headers = {
            "X-Agent-Token": self.token,
            "X-Agent-Host": hostname(),
            "X-Agent-Version": RUNNER_VERSION,
            "User-Agent": "stzb-runner/%s" % RUNNER_VERSION,
        }
        if self.uid:
            # 服务端靠这个头把请求归属到具体客户端；没有它就只能退回用机器名
            headers["X-Agent-Uid"] = self.uid
        if body is not None:
            headers["Content-Type"] = content_type

        tries = max(1, int(retries if retries is not None else self.retries))
        last = ""
        for attempt in range(1, tries + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout,
                                            context=self.ctx) as resp:
                    raw = resp.read()
                self.stats["up" if body is not None else "down"] += 1
                if not want_json:
                    return True, raw
                try:
                    return True, json.loads(raw.decode("utf-8"))
                except Exception:
                    return True, raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                # 4xx 是请求本身的问题，重试没意义，直接放弃
                if 400 <= e.code < 500:
                    self.stats["fail"] += 1
                    return False, "HTTP %d %s" % (e.code, detail)
                last = "HTTP %d %s" % (e.code, detail)
            except ssl.SSLCertVerificationError as e:
                self.stats["fail"] += 1
                return False, ("证书校验失败：%s\n"
                               "（公网传输不做降级校验。检查域名证书是否有效、"
                               "或装上 certifi：pip install certifi）" % e)
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, e)
            if attempt < tries:
                time.sleep(min(2 ** attempt, 6))
        self.stats["fail"] += 1
        return False, last or "未知错误"

    # -------------------------------------------------------------- 探活 / 心跳

    def ping(self) -> Tuple[bool, Any]:
        """兼容老接口的 GET 探活（不带负载）。"""
        return self.request("GET", "/api/agent/ping")

    def heartbeat(self, payload: Dict[str, Any],
                  timeout: int = HEARTBEAT_TIMEOUT) -> Tuple[bool, Any]:
        """带负载的心跳。只重试 1 次 —— 高频小请求，重试反而拖慢下一拍。

        响应里除了常规字段，还可能带着服务端对客户端的指令：
        指派（assignment）、是否需要切换（switch_needed）、一次性指令（command）。
        """
        return self.request("POST", "/api/agent/ping",
                            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            timeout=timeout, retries=1)

    def probe_result(self, uid: str, ok: bool, message: str = "",
                     data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
        """回报人工探测的结果。"""
        payload = {"uid": uid, "ok": bool(ok), "message": str(message or "")[:600],
                   "data": data or {}}
        return self.request("POST", "/api/agent/probe",
                            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            timeout=HEARTBEAT_TIMEOUT, retries=1)

    # -------------------------------------------------------------- 配置

    def fetch_config(self, version: int = 0) -> Tuple[bool, Any]:
        return self.request("GET", "/api/agent/config", params={"version": version})

    # -------------------------------------------------------------- 任务队列

    def list_jobs(self, uid: str = "") -> Tuple[bool, Any]:
        return self.request("GET", "/api/agent/jobs", params={"uid": uid or self.uid})

    def take_job(self, job_id: int) -> bool:
        ok, _ = self.request("POST", "/api/agent/jobs/%d/take" % job_id,
                             params={"host": hostname(), "uid": self.uid},
                             body=b"{}")
        return ok

    def ack_job(self, job_id: int, status: str = "done",
                run_id: Optional[int] = None) -> bool:
        payload = json.dumps({"status": status, "run_id": run_id}).encode("utf-8")
        ok, _ = self.request("POST", "/api/agent/jobs/%d/ack" % job_id, body=payload)
        return ok

    # -------------------------------------------------------------- 上传

    def create_run(self, payload: Dict[str, Any]) -> Tuple[bool, Optional[int], str]:
        ok, data = self.request("POST", "/api/agent/run",
                                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if not ok:
            return False, None, str(data)
        rid = (data or {}).get("run_id") if isinstance(data, dict) else None
        return True, rid, ""

    def upload_report(self, run_id: int, html_path: str) -> Tuple[bool, str]:
        try:
            with open(html_path, "rb") as f:
                content = f.read()
        except Exception as e:
            return False, "读不到报告文件：%r" % (e,)
        body, ctype = encode_multipart(
            {}, [("file", os.path.basename(html_path), content, "text/html")])
        ok, data = self.request("POST", "/api/agent/run/%d/report" % run_id,
                                body=body, content_type=ctype, timeout=180)
        return ok, "" if ok else str(data)

    def upload_shot(self, run_id: int, task_key: str, index: int,
                    jpeg_bytes: bytes, filename: str) -> Tuple[bool, str]:
        body, ctype = encode_multipart(
            {"task_key": task_key, "index": str(index)},
            [("file", filename, jpeg_bytes, "image/jpeg")])
        ok, data = self.request("POST", "/api/agent/run/%d/shot" % run_id,
                                body=body, content_type=ctype, timeout=120)
        return ok, "" if ok else str(data)

    def finish_run(self, run_id: int) -> Tuple[bool, str]:
        ok, data = self.request("POST", "/api/agent/run/%d/done" % run_id, body=b"{}")
        return ok, "" if ok else str(data)


# ------------------------------------------------------------------ 复用本地报告

class LocalReportStub:
    """把 logs/reports/latest.json 包装成 upload_run 认识的对象。

    用途是 `--upload-last`：网络断了 / 服务器当时挂了，跑完没传上去，
    事后不用重跑一遍，直接从本地报告补传即可。
    """

    class _Entry:
        __slots__ = ("key", "name", "shots")

        def __init__(self, key: str, name: str, shots: List[str]):
            self.key = key
            self.name = name
            self.shots = shots

    def __init__(self, data: Dict[str, Any]):
        self._data = data
        try:
            self.started_at = dt.datetime.fromisoformat(
                str(data.get("started_at") or "").replace("Z", ""))
        except Exception:
            self.started_at = dt.datetime.now()
        self.slot = data.get("slot") or ""
        self.entries: List["LocalReportStub._Entry"] = []
        for t in data.get("tasks") or []:
            shots = [p for p in (t.get("shots") or []) if os.path.exists(p)]
            self.entries.append(self._Entry(t.get("key") or "", t.get("name") or "", shots))

    def to_dict(self) -> Dict[str, Any]:
        return self._data


def load_last_report(report_dir: str) -> Optional[Tuple[LocalReportStub, Dict[str, str]]]:
    """读最近的本地报告。返回 (stub, paths) 或 None。"""
    latest_json = os.path.join(report_dir, "latest.json")
    latest_html = os.path.join(report_dir, "latest.html")
    if not os.path.exists(latest_json):
        return None
    try:
        with open(latest_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    # latest.html 可能是拷贝失败的旧版，优先找同名 run_*.html
    html = ""
    if os.path.exists(latest_html):
        html = latest_html
    else:
        stamp = str(data.get("started_at") or "").replace(":", "").replace("-", "")
        for name in sorted(os.listdir(report_dir), reverse=True):
            if name.startswith("run_") and name.endswith(".html") and stamp[:13] in name:
                html = os.path.join(report_dir, name)
                break
    return LocalReportStub(data), {"html": html, "json": latest_json}


# ------------------------------------------------------------------ 图片瘦身

def shrink_to_jpeg(path: str, width: int = 1000, quality: int = 76) -> Optional[bytes]:
    """把截图缩小并转成 JPEG，返回字节。失败返回 None。

    上传原图（3MB 一张）没意义：控制台里只是缩略图预览，
    缩到 1000px + q76 大约 90~150KB，一眼看上去没区别，流量省 20 倍。
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        if w > width:
            img = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            return None
        return buf.tobytes()
    except Exception:
        return None


# ------------------------------------------------------------------ 高层封装

def upload_run(cfg: Dict[str, Any], report, paths: Dict[str, str],
               logger: Callable[[str], None] = print,
               shots_per_task: int = 4,
               identity: Any = None) -> Dict[str, Any]:
    """把一轮结果整体推上去。**任何失败都不抛异常**，只记录到返回值里。

    设计原则：上报告是「尽力而为」。网络断了、服务器挂了，也不能让已经跑完的
    任务白跑 —— 本机 logs/reports/ 里那份报告始终是完整的。
    """
    cloud = (cfg.get("cloud") or {})
    # ★ 2026-09-20：cloud.enabled 已废弃（独立模式移除）。判定统一为「地址 + 令牌齐不齐」，
    #   与 run_daily.resolve_mode 保持同一口径 —— 否则会出现「脚本认为托管、上传认为没启用」
    #   这种两边打架的情况。
    bound = bool(str(cloud.get("base_url") or "").strip()
                 and str(cloud.get("token") or "").strip())
    result: Dict[str, Any] = {"enabled": bound,
                              "ok": False, "run_id": None, "error": "",
                              "shots_ok": 0, "shots_fail": 0, "report_ok": False}
    if not bound:
        return result
    if not report:
        result["error"] = "没有可上传的报告对象"
        return result

    try:
        client = CloudClient(base_url=cloud.get("base_url", ""),
                             token=cloud.get("token", ""),
                             timeout=int(cloud.get("timeout", DEFAULT_TIMEOUT)),
                             retries=int(cloud.get("retries", DEFAULT_RETRIES)),
                             logger=logger,
                             uid=getattr(identity, "uid", "") or "")
    except CloudError as e:
        result["error"] = str(e)
        logger("!! 后台上传未启用：%s" % e)
        return result

    payload = report.to_dict()
    payload["host"] = getattr(identity, "host", "") or hostname()
    payload["uid"] = getattr(identity, "uid", "") or ""
    payload["runner_version"] = RUNNER_VERSION
    payload["client_run_id"] = "%s-%s-%s" % (
        payload["host"],
        report.started_at.strftime("%Y%m%dT%H%M%S"),
        report.slot or "auto")
    # 客户端自报的账号/角色标签：服务端以自己当时的指派为准入库，
    # 但会拿这份标签去核对「切换到底生效了没」，所以必须如实上报。
    payload["account_label"] = getattr(report, "account_label", "") or ""
    payload["role_label"] = getattr(report, "role_label", "") or ""

    ok, rid, err = client.create_run(payload)
    if not ok or not rid:
        result["error"] = err or "服务器没有返回 run_id"
        logger("  × 上传运行记录失败：%s（本地报告不受影响）" % result["error"])
        return result
    result["run_id"] = rid
    logger("  · 已上传运行记录 → 运行 #%d" % rid)

    html_path = paths.get("html") or ""
    if html_path and os.path.exists(html_path):
        ok, err = client.upload_report(rid, html_path)
        result["report_ok"] = ok
        size_mb = os.path.getsize(html_path) / 1048576.0
        if ok:
            logger("  · 已上传报告（%.1f MB）" % size_mb)
        else:
            logger("  × 上传报告失败：%s" % err)

    # 截图：每个任务挑几张，缩过再传
    from .report import pick_shots
    for entry in report.entries:
        if not entry.shots:
            continue
        for i, p in enumerate(pick_shots(entry.shots, limit=shots_per_task)):
            jpg = shrink_to_jpeg(p)
            if jpg is None:
                result["shots_fail"] += 1
                continue
            name = "%s_%02d.jpg" % (entry.key, i + 1)
            ok, err = client.upload_shot(rid, entry.key, i, jpg, name)
            if ok:
                result["shots_ok"] += 1
            else:
                result["shots_fail"] += 1
    if result["shots_ok"] or result["shots_fail"]:
        logger("  · 截图上传：成功 %d 张，失败 %d 张"
               % (result["shots_ok"], result["shots_fail"]))

    client.finish_run(rid)
    result["ok"] = True
    base = client.base_url
    logger("  ✓ 后台控制台：%s/runs/%d" % (base, rid))
    return result
