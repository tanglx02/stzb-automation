# -*- coding: utf-8 -*-
"""采集端（脚本）用的 API。整个路由组统一挂 agent 令牌校验，不可能漏。
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, Header, HTTPException,
                     UploadFile)

from . import db, managed, security, settings
from .schemas import AckIn, RunIn

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


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


# ------------------------------------------------------------------ 基础

@router.get("/ping")
def ping(x_agent_version: Optional[str] = Header(default=None),
         x_agent_host: Optional[str] = Header(default=None)):
    cur = db.config_current()
    if x_agent_host:
        db.kv_set("last_agent_host", x_agent_host)
    if x_agent_version:
        db.kv_set("last_agent_version", x_agent_version)
    return {
        "ok": True,
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "config_version": cur["version"],
        "allow_run_requests": settings.ALLOW_RUN_REQUESTS,
        "agent_version": x_agent_version,
        "host": x_agent_host,
    }


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
def jobs(limit: int = 10):
    if not settings.ALLOW_RUN_REQUESTS:
        return {"jobs": []}
    rows = db.request_pending(limit=max(1, min(limit, 50)))
    return {"jobs": [
        {"id": r["id"], "slot": r["slot"], "only": r["only_tasks"] or "",
         "dry_run": bool(r["dry_run"]), "created_at": r["created_at"],
         "created_by": r["created_by"], "note": r["note"] or ""}
        for r in rows]}


@router.post("/jobs/{req_id}/take")
def job_take(req_id: int, host: str = ""):
    if not db.request_take(req_id, host or "unknown"):
        raise HTTPException(status_code=409, detail="该任务已被领取或已取消")
    db.event("info", "agent", "领取待执行任务 #%d（%s）" % (req_id, host))
    return {"ok": True, "id": req_id}


@router.post("/jobs/{req_id}/ack")
def job_ack(req_id: int, body: AckIn):
    status = body.status if body.status in ("done", "failed", "cancelled") else "done"
    db.request_finish(req_id, status, body.run_id)
    db.event("info", "agent", "任务 #%d 回执 %s（run=%s）" % (req_id, status, body.run_id))
    return {"ok": True}


# ------------------------------------------------------------------ 上传运行结果

@router.post("/run")
def create_run(body: RunIn, x_agent_host: Optional[str] = Header(default=None)):
    counts = body.counts or {}
    if counts:
        n_ok = int(counts.get("ok") or 0)
        n_fail = int(counts.get("fail") or 0)
        n_skip = int(counts.get("skip") or 0)
    else:
        n_ok = sum(1 for t in body.tasks if t.status == "ok")
        n_fail = sum(1 for t in body.tasks if t.status in ("fail", "error"))
        n_skip = sum(1 for t in body.tasks if t.status == "skip")

    rid = db.run_create({
        "client_run_id": body.client_run_id,
        "host": body.host or x_agent_host, "slot": body.slot, "dry_run": body.dry_run,
        "started_at": body.started_at, "finished_at": body.finished_at,
        "duration_seconds": body.duration_seconds,
        "n_ok": n_ok, "n_fail": n_fail, "n_skip": n_skip,
        "all_ok": body.all_ok, "env": body.env, "notes": body.notes,
        "runner_version": body.runner_version, "exit_code": body.exit_code,
    })
    db.run_replace_tasks(rid, [t.model_dump() for t in body.tasks])
    db.event("info", "agent", "收到运行记录 #%d（%s 档，成功%d 失败%d 跳过%d）"
             % (rid, body.slot or "-", n_ok, n_fail, n_skip))
    return {"ok": True, "run_id": rid, "url": "/runs/%d" % rid}


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
