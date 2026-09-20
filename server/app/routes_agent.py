# -*- coding: utf-8 -*-
"""采集端（脚本）用的 API。整个路由组统一挂 agent 令牌校验，不可能漏。

**拓扑前提（决定了这里所有接口的形状）：**

默认部署是「内网一台机器同时跑后端 + 采集脚本」，但也支持后端在别处、
采集端在内网 —— 无论哪种，都可能出现**后端连不上采集端**的情况（NAT、
防火墙、不在同一网段）。所以这里所有接口都是「采集端主动来问」的形状：

  · 「客户端是否在线」= 客户端主动上报心跳，后端记 last_seen（见 /ping）
  · 「探测客户端当前界面」= 后端把指令挂在客户端那一行上，客户端下次心跳
    带回去执行，再通过 /probe 回报（见 db.client_request_probe）
  · 「给某台客户端派任务」= 写进 run_requests.client_id，心跳时按 uid 过滤着领

**实时长连接（/ws）只是「加速器」，不改上面这套形状。** 它做的是：客户端常驻时
先建一条 WebSocket，后端有派任务 / 指令 / 改指派就顺手喊一声（毫秒到）；
客户端收到后**照常走上面的 HTTP 接口**取数据，而不是从推送里取。

之所以不把推送当数据源，是因为推送是**尽力而为**的：消息会丢、连接会断，
只有数据库是唯一事实来源。这样断开最多让事情慢一拍，绝不会丢活 ——
以后想把后端搬到公网（或换长轮询 / 消息队列），这套接口一个字都不用改。

★ 状态计算只有一份：心跳和长连接共用 `build_client_state()`。
  两处各写一遍迟早分叉，后果是「同一台客户端走哪条通道拿到不一样的指派」，
  只在一条通道上复现、极难排查。护栏测试 test_realtime_contract 盯着这条。
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from typing import Any, Dict, Optional

from fastapi import (APIRouter, Depends, File, Form, Header, HTTPException,
                     Request, UploadFile)

from . import db, managed, plan, security, settings
from .schemas import AckIn, HeartbeatIn, ProbeResultIn, RunIn

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# ★ 与客户端 stzb/realtime.py 的 BUSY_PREFIX **必须一致**（自检里有断言钉着）。
#   含义：客户端此刻正在跑任务，没法安全地同时操作游戏界面 →
#   这条指令不是「失败」，而是「先放回队列，等我闲下来」。
BUSY_PREFIX = "BUSY:"


def require_agent(x_agent_token: Optional[str] = Header(default=None),
                  authorization: Optional[str] = Header(default=None)) -> None:
    """令牌可以放在 X-Agent-Token，也可以放 Authorization: Bearer <token>。"""
    token = x_agent_token or ""
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not security.check_agent_token(token):
        db.event("warn", "agent", "agent 令牌校验失败")
        raise HTTPException(status_code=401, detail="invalid agent token")


# 整组路由统一鉴权：以后新增接口忘了加，也不会裸奔
router = APIRouter(prefix="/api/agent", tags=["agent"],
                   dependencies=[Depends(require_agent)])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _ident(request: Request) -> Dict[str, Any]:
    """从请求头里取客户端身份（老客户端只发头、不发 body 也能用）。"""
    return {
        "uid": (request.headers.get("x_agent_uid") or "").strip(),
        "host": (request.headers.get("x_agent_host") or "").strip(),
        "version": (request.headers.get("x_agent_version") or "").strip(),
    }


def _role_key(name: Any) -> str:
    """角色名的归一化比较键（薄封装，实现在 db.role_key）。

    **角色只按名字识别，绝不比较区服** —— 游戏一合服，区服编号就变了
    （X6014 → X6021），拿旧编号去比会把「其实已经是同一个角色」误判成
    「需要切换」，客户端于是天天白切一遍。
    """
    return db.role_key(name)


def _safe_name(name: str, fallback: str = "file.bin") -> str:
    """只保留安全字符，避免路径穿越；空名给个兜底。"""
    base = os.path.basename(name or "").strip()
    base = _SAFE.sub("_", base).strip("._-")
    return base[:120] if base else fallback


def _run_dir(run_id: int) -> str:
    d = os.path.join(str(settings.ARTIFACT_DIR), str(run_id))
    os.makedirs(d, exist_ok=True)
    return d


def _rm_tree(path: str) -> None:
    try:
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                except OSError:
                    pass
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
        os.rmdir(path)
    except OSError:
        pass


def _save_upload(run_id: int, up: UploadFile, *, kind: str, label: str,
                 max_bytes: int) -> dict:
    """分块写盘，边写边核对大小；超限立刻中止并删掉半截文件。"""
    d = _run_dir(run_id)
    orig = up.filename or ""
    name = _safe_name(orig, fallback="%s_%s.bin" % (kind, secrets.token_hex(4)))
    path = os.path.join(d, name)
    h = hashlib.sha256()
    size = 0
    ok = False
    try:
        with open(path, "wb") as f:
            while True:
                chunk = up.file.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413,
                                        detail="文件超过上限 %d 字节" % max_bytes)
                h.update(chunk)
                f.write(chunk)
        ok = True
    finally:
        try:
            up.file.close()
        except Exception:
            pass
        if not ok:
            try:
                os.remove(path)
            except OSError:
                pass

    aid = db.artifact_add(run_id, kind, name, label=label, original=orig,
                          mime=up.content_type or "", size=size, sha256=h.hexdigest())
    return {"id": aid, "filename": name, "size": size, "kind": kind, "label": label}


# ------------------------------------------------------ 客户端状态（共用）

def build_client_state(cli: Dict[str, Any],
                       status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把「这台客户端现在该收到什么」算出来。

    HTTP 心跳（/ping）与实时通道（WebSocket）**共用这一份实现** ——
    两处各写一遍迟早会分叉，而分叉的后果是「同一台客户端走不同通道
    拿到不同的指派」，出现时极难排查。所以谁也别复制粘贴这段逻辑。

    返回的就是客户端要的那一坨：指派（assignment）、是否需要切换
    （switch_needed）、一次性指令（command）、有没有待领任务。
    """

    # ---- 该用哪个账号/角色 ----
    aid = cli.get("account_id")
    rid = cli.get("role_id")
    acc = db.account_get(int(aid)) if aid else None
    role = db.role_get(int(rid)) if rid else None

    # 账号要没停用，否则相当于没指派
    if acc and not acc.get("enabled"):
        acc, role = None, None
    if role and not role.get("enabled"):
        role = None
    # 角色必须挂在被指派的账号下，否则这种脏数据直接忽略
    if role and acc and int(role["account_id"]) != int(acc["id"]):
        role = None

    target = None
    if acc:
        target = {
            "account_id": acc["id"],
            "label": acc["label"],
            "login_name": acc.get("login_name") or "",
            "masked": acc.get("masked") or "",
            "role_id": (role or {}).get("id"),
            "role": (role or {}).get("name") or "",
            # ★ 区服/赛季只作备注下发：切换角色时**只认角色名**。
            #   区服会随合服变化（X6014 合服后可能改名），拿它当识别依据迟早失效。
            "server": (role or {}).get("server") or "",
            "season": (role or {}).get("season") or "",
            "tab": (role or {}).get("tab") or "",
        }
        # 该角色的「执行任务模式」：本轮跑哪些任务、在哪些档位跑
        if role:
            tp = plan.loads_plan(role.get("task_plan"))
            target["task_plan"] = tp
            if tp["mode"] == plan.MODE_PAUSED:
                target["paused"] = True

    # ---- 需不需要切？----
    # 判定很保守：客户端报上来的「脱敏账号」或「角色名」跟目标对不上就让它切。
    # 客户端那边还会自己再比一次（它知道自己当前的真实状态）。
    #
    # ★ 角色一律**按名字**比 —— 区服会被游戏改掉（合服），角色名不会。
    switch_needed = False
    reason = ""
    if target:
        if target.get("paused"):
            # 角色被设为「暂停执行」：连切换都不必做，切过去也是白跑
            switch_needed = False
            reason = "角色当前被设为「暂停执行」，本轮不切换"
        else:
            cur = (status or {}).get("current") or {}
            if cli.get("probe", {}).get("force_switch"):
                switch_needed = True
                reason = "管理端要求强制切换"
            elif not cur.get("masked") and not cur.get("role"):
                # 客户端还没进游戏、状态未知 —— 不算「不一致」，让它照常启动
                switch_needed = False
                reason = "客户端尚未上报当前账号/角色"
            else:
                if target.get("masked") and cur.get("masked") \
                        and str(target["masked"]) != str(cur["masked"]):
                    switch_needed = True
                    reason = "账号不一致（目标 %s，当前 %s）" % (target["masked"], cur["masked"])
                elif target.get("role") and cur.get("role") \
                        and _role_key(target["role"]) != _role_key(cur["role"]):
                    switch_needed = True
                    reason = "角色不一致（目标 %s，当前 %s）" % (target["role"], cur["role"])

    # ---- 一次性指令 ----
    command = db.client_probe_take(int(cli["id"])) or None

    # 强制切换这条指令要顺带把「切换意图」传给客户端
    if command and command.get("kind") == "switch":
        switch_needed = True
        reason = reason or "管理端要求强制切换"

    cur_cfg = db.config_current()
    jobs = len(db.request_pending_for(limit=99, client_id=int(cli["id"])))

    return {
        "ok": True,
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "server_time": db.now(),
        # 心跳节奏由服务端定，方便以后统一调；客户端照它 sleep
        "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
        "offline_after": settings.CLIENT_OFFLINE_AFTER,
        "client": {"id": cli["id"], "name": cli.get("name"),
                   "enabled": bool(cli.get("enabled"))},
        "config_version": cur_cfg["version"],
        "allow_run_requests": settings.ALLOW_RUN_REQUESTS,
        "registered": True,
        "task_paused": not bool(cli.get("enabled")),
        "pending_jobs": jobs,
        "assignment": target,
        "switch_needed": switch_needed,
        "switch_reason": reason,
        "command": command,
    }


# ------------------------------------------------------------------ 基础

@router.get("/ping")
def ping(request: Request,
         x_agent_version: Optional[str] = Header(default=None),
         x_agent_host: Optional[str] = Header(default=None),
         x_agent_uid: Optional[str] = Header(default=None)):
    """探活 / 心跳。GET 版（不带负载），老客户端兼容。

    只做「我还在」这一件事：登记客户端、刷新 last_seen。返回值里的
    `assignment` 让客户端启动时就知道自己该用哪个账号/角色。
    """
    return _heartbeat(request, None, x_agent_version, x_agent_host, x_agent_uid)


@router.post("/ping")
def ping_post(request: Request, body: HeartbeatIn):
    """带负载的心跳。客户端后台线程每隔 N 秒发一次，附带当前状态。

    返回值是**服务端对客户端的指令通道**，包含：
      · config_version 变了没（客户端可据此决定要不要重拉配置）
      · assignment     该用哪个账号 / 哪个角色（换了指派客户端会自动切）
      · switch_needed  true 表示「你当前跑的账号/角色和目标不一致，切一下」
      · command        一次性指令（人工点的「探测」「强制切换」「立刻跑一轮」）
    """
    return _heartbeat(request, body, body.host and None, None, None)


def _heartbeat(request: Request, body: Optional[HeartbeatIn],
               hdr_version: Optional[str], hdr_host: Optional[str],
               hdr_uid: Optional[str]) -> Dict[str, Any]:
    ident = _ident(request)
    uid = (body.uid if body and body.uid else None) or ident["uid"] or hdr_uid or ""
    host = (body.host if body and body.host else None) or ident["host"] or hdr_host or ""
    version = (body and None) or ident["version"] or hdr_version or ""

    # 老客户端（只发头、连 uid 都没有）拿 host 当标识，至少还能被看见
    uid = uid or host
    if not uid:
        # 实在没有标识就退化：不发 token 的探活不该污染客户端列表
        cur = db.config_current()
        return {"ok": True, "app": settings.APP_NAME, "version": settings.APP_VERSION,
                "config_version": cur["version"],
                "allow_run_requests": settings.ALLOW_RUN_REQUESTS,
                "server_time": db.now(),
                "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
                "registered": False,
                "note": "本次心跳没带 uid，未登记为客户端"}

    status: Optional[Dict[str, Any]] = None
    if body is not None:
        status = {
            "mode": body.mode or "",
            "state": body.state or "",
            "busy": bool(body.busy),
            "note": (body.note or "")[:300],
            "current": (body.current.model_dump() if body.current else {}),
            "device": (body.device.model_dump() if body.device else {}),
            "last_run_at": body.last_run_at or "",
            "extra": body.extra or {},
        }

    if hdr_host:
        db.kv_set("last_agent_host", hdr_host)
    if hdr_version:
        db.kv_set("last_agent_version", hdr_version)

    cli = db.client_upsert(uid, host=host, agent_version=version,
                           ip=_client_ip(request), status=status)
    if not cli:
        raise HTTPException(status_code=400, detail="无法登记客户端")

    # 具体算什么，见 build_client_state()：HTTP 心跳与实时通道共用
    return build_client_state(cli, status)


@router.post("/probe")
def probe_result(request: Request, body: ProbeResultIn,
                 x_agent_uid: Optional[str] = Header(default=None),
                 x_agent_host: Optional[str] = Header(default=None)):
    """客户端上报人工探测的结果（界面文字、截图摘要、切换成败）。

    **`BUSY:` 前缀是一种控制信号，不是普通失败**：客户端正在跑任务时没法安全地
    同时操作游戏界面，于是回报 `BUSY:…`；这里把指令**放回待执行**，
    等它闲下来再取走。没有这个分支的话，管理端点「探测」时客户端恰好忙，
    指令就会被消费掉且永远不执行 —— 而界面上还显示「已下发」，很误导人。
    （客户端侧发出这个前缀的地方见 stzb/realtime.py 的 BUSY_PREFIX。）
    """
    ident = _ident(request)
    uid = body.uid or ident["uid"] or x_agent_uid or ident["host"] or x_agent_host or ""
    cli = db.client_by_uid(uid)
    if not cli:
        raise HTTPException(status_code=404, detail="这台客户端还没登记过（先发一次心跳）")
    msg = str(body.message or "")
    if not body.ok and msg.startswith(BUSY_PREFIX):
        back = db.client_probe_requeue(int(cli["id"]), note=msg[len(BUSY_PREFIX):].strip())
        db.event("info", "agent", "客户端正忙，指令已放回队列（%s）%s"
                 % (cli.get("name") or uid, "：%s" % msg[len(BUSY_PREFIX):].strip()
                    if msg[len(BUSY_PREFIX):].strip() else ""))
        return {"ok": True, "requeued": bool(back)}
    db.client_probe_finish(int(cli["id"]), bool(body.ok), msg, body.data)
    return {"ok": True, "requeued": False}


# ------------------------------------------------------------------ 配置

@router.get("/config")
def get_config(version: int = 0):
    cur = db.config_current()
    if not cur["payload"]:
        return {"version": 0, "payload": managed.DEFAULT_MANAGED_CONFIG,
                "updated_at": None, "note": "服务端未配置，使用内置默认值",
                "changed": False}
    return {"version": cur["version"], "payload": cur["payload"],
            "updated_at": cur["updated_at"], "note": cur["note"],
            "changed": int(version) != int(cur["version"])}


# ------------------------------------------------------------------ 待执行任务

@router.get("/jobs")
def jobs(request: Request, limit: int = 10,
         x_agent_uid: Optional[str] = Header(default=None),
         x_agent_host: Optional[str] = Header(default=None),
         uid: str = ""):
    """待领取任务。**按客户端过滤**：公共任务（没指定客户端）谁都能领，
    定向任务只有被点名的那台能领到。

    这是多客户端分发的核心 —— 客户端不需要在本地做任何筛选逻辑。
    """
    if not settings.ALLOW_RUN_REQUESTS:
        return {"jobs": []}
    ident = _ident(request)
    key = uid or ident["uid"] or x_agent_uid or ident["host"] or x_agent_host or ""
    cli = db.client_by_uid(key) if key else None
    if cli and not cli.get("enabled"):
        return {"jobs": [], "paused": True,
                "note": "这台客户端在管理端被暂停了派发"}
    cid = int(cli["id"]) if cli else None
    # 未登记的客户端只拿得到公共任务 —— 定向任务不能漏给它
    rows = db.request_pending_for(limit=max(1, min(limit, 50)), client_id=cid)
    return {"jobs": [
        {"id": r["id"], "slot": r["slot"], "only": r["only_tasks"] or "",
         "dry_run": bool(r["dry_run"]), "created_at": r["created_at"],
         "created_by": r["created_by"], "note": r["note"] or "",
         "client_id": r["client_id"]}
        for r in rows], "client_id": cid, "registered": cli is not None}


@router.post("/jobs/{req_id}/take")
def job_take(req_id: int, request: Request, host: str = "",
             uid: str = "", x_agent_uid: Optional[str] = Header(default=None)):
    """领取任务。这里要再验一次归属 —— 否则 A 客户端猜到 id 就能抢走 B 的定向任务。"""
    ident = _ident(request)
    key = uid or ident["uid"] or x_agent_uid or host or ident["host"] or ""
    cli = db.client_by_uid(key) if key else None

    with db.tx() as c:
        row = c.execute("SELECT client_id, status FROM run_requests WHERE id=?",
                        (req_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="没有这条任务")
    owner = row["client_id"]
    if owner is not None and (not cli or int(cli["id"]) != int(owner)):
        raise HTTPException(status_code=403, detail="这条任务是派给别的客户端的")

    if not db.request_take(req_id, (cli or {}).get("name") or host or "unknown"):
        raise HTTPException(status_code=409, detail="该任务已被领取或已取消")
    db.event("info", "agent", "领取待执行任务 #%d（%s）"
             % (req_id, (cli or {}).get("name") or host or "unknown"))
    return {"ok": True, "id": req_id, "client_id": (cli or {}).get("id")}


@router.post("/jobs/{req_id}/ack")
def job_ack(req_id: int, body: AckIn):
    status = body.status if body.status in ("done", "failed", "cancelled") else "done"
    db.request_finish(req_id, status, body.run_id)
    db.event("info", "agent", "任务 #%d 回执 %s（run=%s）" % (req_id, status, body.run_id))
    return {"ok": True}


# ------------------------------------------------------------------ 上传运行结果

@router.post("/run")
def create_run(request: Request, body: RunIn,
               x_agent_host: Optional[str] = Header(default=None),
               x_agent_uid: Optional[str] = Header(default=None)):
    counts = body.counts or {}
    if counts:
        n_ok = int(counts.get("ok") or 0)
        n_fail = int(counts.get("fail") or 0)
        n_skip = int(counts.get("skip") or 0)
    else:
        n_ok = sum(1 for t in body.tasks if t.status == "ok")
        n_fail = sum(1 for t in body.tasks if t.status in ("fail", "error"))
        n_skip = sum(1 for t in body.tasks if t.status == "skip")

    # 归属到客户端与账号：优先用上报的 uid，退回 host。
    # 账号/角色以**服务端当时的指派**为准（比客户端自报的可信），
    # 但同时存一份客户端自报的标签，用于人工核对切换是否真的生效。
    ident = _ident(request)
    key = body.uid or ident["uid"] or x_agent_uid or body.host or ident["host"] \
        or x_agent_host or ""
    cli = db.client_by_uid(key) if key else None
    aid = int(cli["account_id"]) if cli and cli.get("account_id") else None
    rid = int(cli["role_id"]) if cli and cli.get("role_id") else None
    acc = db.account_get(aid) if aid else None
    role = db.role_get(rid) if rid else None
    # 服务端没指派过角色时，按**角色名**把这条记录归位 —— 这样即使没配置指派，
    # 「角色执行」页也能看到这个角色每天跑得怎么样。
    if role is None and (body.role_label or "").strip():
        hit = db.role_find_by_name(body.role_label)
        if hit:
            role = hit
            rid = int(hit["id"])
            if acc is None and hit.get("account_id"):
                acc = db.account_get(int(hit["account_id"]))

    rid_run = db.run_create({
        "client_run_id": body.client_run_id,
        "host": body.host or x_agent_host or (cli or {}).get("host"),
        "client_id": (cli or {}).get("id"),
        "account_id": (acc or {}).get("id"),
        "role_id": (role or {}).get("id"),
        "account_label": (acc or {}).get("label") or body.account_label or "",
        "role_label": (role or {}).get("name") or body.role_label or "",
        "slot": body.slot, "dry_run": body.dry_run,
        "started_at": body.started_at, "finished_at": body.finished_at,
        "duration_seconds": body.duration_seconds,
        "n_ok": n_ok, "n_fail": n_fail, "n_skip": n_skip,
        "all_ok": body.all_ok, "env": body.env, "notes": body.notes,
        "runner_version": body.runner_version, "exit_code": body.exit_code,
    })
    db.run_replace_tasks(rid_run, [t.model_dump() for t in body.tasks])
    db.event("info", "agent", "收到运行记录 #%d（%s 档，成功%d 失败%d 跳过%d，%s%s）"
             % (rid_run, body.slot or "-", n_ok, n_fail, n_skip,
                (cli or {}).get("name") or body.host or "未知客户端",
                (" / " + str((acc or {}).get("label"))) if acc else ""))
    return {"ok": True, "run_id": rid_run, "url": "/runs/%d" % rid_run}


@router.post("/run/{run_id}/report")
def upload_report(run_id: int, file: UploadFile = File(...)):
    if not db.run_get(run_id):
        raise HTTPException(status_code=404, detail="没有这条运行记录")
    info = _save_upload(run_id, file, kind="report", label="report",
                        max_bytes=settings.MAX_UPLOAD_BYTES)
    db.event("info", "agent", "运行 #%d 上传报告 %s（%.1f MB）"
             % (run_id, info["filename"], info["size"] / 1048576.0))
    return {"ok": True, **info}


@router.post("/run/{run_id}/shot")
def upload_shot(run_id: int, file: UploadFile = File(...),
                task_key: str = Form(default=""), index: int = Form(default=0)):
    if not db.run_get(run_id):
        raise HTTPException(status_code=404, detail="没有这条运行记录")
    existing = sum(1 for a in db.artifact_list(run_id) if a["kind"] == "shot")
    if existing >= settings.MAX_SHOTS_PER_RUN:
        raise HTTPException(status_code=429,
                            detail="本轮截图已达上限 %d 张" % settings.MAX_SHOTS_PER_RUN)
    key = _safe_name(task_key, fallback="task")[:40]
    orig = file.filename or ""
    ext = os.path.splitext(orig)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    # 名字里带任务 key 和序号，落到磁盘上还能一眼看出归属
    file.filename = "%s_%02d%s" % (key, index + 1, ext)
    info = _save_upload(run_id, file, kind="shot", label=task_key,
                        max_bytes=settings.MAX_UPLOAD_BYTES)
    return {"ok": True, **info}


@router.post("/run/{run_id}/done")
def run_done(run_id: int):
    if not db.run_get(run_id):
        raise HTTPException(status_code=404, detail="没有这条运行记录")
    dropped = db.prune_runs(settings.KEEP_RUNS)
    for i in dropped:
        _rm_tree(os.path.join(str(settings.ARTIFACT_DIR), str(i)))
    db.event("info", "agent", "运行 #%d 收尾完成，清理旧记录 %d 条" % (run_id, len(dropped)))
    return {"ok": True, "run_id": run_id, "pruned": len(dropped)}
