# -*- coding: utf-8 -*-
"""管理端：页面 + 表单动作。服务端渲染，没有前端构建步骤。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, managed, security, settings

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

router = APIRouter(tags=["console"])


# ------------------------------------------------------------------ 鉴权

def current_user(request: Request) -> Optional[str]:
    return request.session.get("user")


def require_admin(request: Request) -> str:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return u


def _ctx(request: Request, **kw) -> Dict[str, Any]:
    base = {
        "request": request,
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "user": current_user(request),
        "task_meta": managed.TASK_META,
        "status_label": managed.STATUS_LABEL,
    }
    base.update(kw)
    return base


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ------------------------------------------------------------------ 登录

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, err: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", _ctx(request, err=err))


@router.post("/login")
async def login_submit(request: Request):
    ip = _client_ip(request)
    blocked, wait = security.login_blocked(ip)
    if blocked:
        db.event("warn", "console", "登录被限流（ip=%s）" % ip)
        return templates.TemplateResponse(request, 
            "login.html", _ctx(request, err="尝试次数过多，请 %d 秒后再试" % wait),
            status_code=429)

    form = await request.form()
    user = str(form.get("user") or "")
    pwd = str(form.get("password") or "")
    if not security.check_admin(user, pwd):
        security.login_record_fail(ip)
        db.event("warn", "console", "登录失败（ip=%s，用户=%s）" % (ip, user))
        return templates.TemplateResponse(request, 
            "login.html", _ctx(request, err="用户名或口令不对"), status_code=401)

    security.login_reset(ip)
    request.session["user"] = user.strip()
    db.event("info", "console", "登录成功（ip=%s）" % ip)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ 仪表盘

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    require_admin(request)
    stats = db.run_stats(days=14)
    runs = db.run_list(limit=8)
    pending = db.request_pending(limit=5)
    cur = db.config_current()
    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        request, nav="dash", stats=stats, runs=runs, pending=pending,
        config_version=cur["version"],
        config_updated=cur["updated_at"],
        agent_hint=db.kv_get("agent_token_hint") or "未初始化",
        last_agent_host=db.kv_get("last_agent_host") or "—",
    ))


# ------------------------------------------------------------------ 运行记录

@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, page: int = 1, bad: int = 0, deleted: int = 0):
    require_admin(request)
    page = max(1, page)
    per = 25
    rows = db.run_list(limit=per, offset=(page - 1) * per, only_failed=bool(bad))
    return templates.TemplateResponse(request, "runs.html", _ctx(
        request, nav="runs", runs=rows, page=page, per=per, total=db.run_count(),
        bad=bool(bad), deleted=deleted))


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    require_admin(request)
    row = db.run_get(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="没有这条运行记录")
    arts = db.artifact_list(run_id)
    report = next((a for a in arts if a["kind"] == "report"), None)

    shots_by_key: Dict[str, List] = {}
    for a in arts:
        if a["kind"] == "shot":
            shots_by_key.setdefault(a["label"] or "", []).append(a)

    tasks = []
    for t in db.run_tasks(run_id):
        tasks.append({
            "key": t["key"], "name": t["name"], "status": t["status"],
            "seconds": t["seconds"], "reason": t["reason"],
            "notes": json.loads(t["notes_json"] or "[]"),
            "shots": shots_by_key.get(t["key"], []),
        })
    orphan = shots_by_key.get("", [])

    return templates.TemplateResponse(request, "run_detail.html", _ctx(
        request, nav="runs", run=row, tasks=tasks, report=report, orphan=orphan,
        env=json.loads(row["env_json"] or "{}"),
        notes=json.loads(row["notes_json"] or "[]"),
        total_shots=sum(len(v) for v in shots_by_key.values()),
    ))


@router.post("/runs/{run_id}/delete")
def run_delete(request: Request, run_id: int):
    require_admin(request)
    row = db.run_delete(run_id)
    if row:
        from .routes_agent import _rm_tree
        _rm_tree(os.path.join(str(settings.ARTIFACT_DIR), str(run_id)))
        db.event("info", "console", "删除运行记录 #%d" % run_id)
    return RedirectResponse("/runs?deleted=1", status_code=303)


@router.get("/runs/{run_id}/report")
def run_report(request: Request, run_id: int):
    """把上传的报告 HTML 原样吐出来，但用**严格 CSP** 锁死。

    报告是我们自己生成的（内联 <style> + base64 图片，没有脚本），
    但它毕竟是「上传来的文件」。所以：
      · script 全禁 —— 即使有人往报告里塞了 <script> 也执行不了
      · 只允许 data: 图片和内联样式
      · 页面本身放进 iframe 里看，避免影响控制台
    """
    require_admin(request)
    art = next((a for a in db.artifact_list(run_id)
                if a["kind"] == "report"), None)
    if not art:
        raise HTTPException(status_code=404, detail="这条运行没有报告")
    path = os.path.join(str(settings.ARTIFACT_DIR), str(run_id), art["filename"])
    if not os.path.exists(path):
        raise HTTPException(status_code=410, detail="报告文件已被清理")
    with open(path, "rb") as f:
        body = f.read()
    return Response(content=body, media_type="text/html; charset=utf-8", headers={
        "Content-Security-Policy":
            "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
            "font-src data:; form-action 'none'; base-uri 'none'; frame-ancestors 'self'",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/artifacts/{aid}")
def artifact_file(request: Request, aid: int):
    require_admin(request)
    art = db.artifact_get(aid)
    if not art:
        raise HTTPException(status_code=404, detail="没有这个文件")
    path = os.path.join(str(settings.ARTIFACT_DIR), str(art["run_id"]), art["filename"])
    if not os.path.exists(path):
        raise HTTPException(status_code=410, detail="文件已被清理")
    with open(path, "rb") as f:
        body = f.read()
    return Response(content=body, media_type=art["mime"] or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ 配置

def _flatten(obj: Any, prefix: str = "") -> List[Dict[str, Any]]:
    """把配置摊平成表单能渲染的字段列表。只处理 bool/int/float/str/list[str]。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        if str(k).startswith("_"):
            continue
        path = "%s.%s" % (prefix, k) if prefix else k
        if isinstance(v, bool):
            out.append({"path": path, "kind": "bool", "value": v})
        elif isinstance(v, int) and not isinstance(v, bool):
            out.append({"path": path, "kind": "int", "value": v})
        elif isinstance(v, float):
            out.append({"path": path, "kind": "float", "value": v})
        elif isinstance(v, str):
            out.append({"path": path, "kind": "str", "value": v})
        elif isinstance(v, list):
            out.append({"path": path, "kind": "list",
                        "value": "\n".join(str(x) for x in v)})
        elif isinstance(v, dict):
            out.extend(_flatten(v, path))
    return out


def _set_path(obj: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _coerce(old: Any, raw: Any, kind: str) -> Any:
    if kind == "bool":
        return bool(raw)
    if kind == "int":
        try:
            return int(str(raw).strip())
        except Exception:
            return old
    if kind == "float":
        try:
            return float(str(raw).strip())
        except Exception:
            return old
    if kind == "list":
        if isinstance(raw, str):
            return [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]
        return old
    # str 保持原类型：老值不是 str 就退回老值，避免把名字写成数字
    return str(raw) if isinstance(old, str) else str(raw)


@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, saved: int = 0, err: str = "", restored: int = 0):
    require_admin(request)
    cur = db.config_current()
    payload = cur["payload"] or managed.DEFAULT_MANAGED_CONFIG
    merged = json.loads(json.dumps(managed.DEFAULT_MANAGED_CONFIG))
    for sec, val in payload.items():
        if isinstance(val, dict):
            merged.setdefault(sec, {})
            if isinstance(merged[sec], dict):
                merged[sec].update(val)
            else:
                merged[sec] = val
    fields = _flatten(merged)
    grouped: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    for f in fields:
        top = f["path"].split(".")[0]
        if top not in index:
            # 键名故意不叫 items —— Jinja 里 `g.items` 会解析成字典的 items 方法，
            # 而不是这个键的值，直接报 unhashable type: 'dict'。踩过。
            g = {"section": top, "label": managed.SECTION_LABEL.get(top, top), "rows": []}
            index[top] = g
            grouped.append(g)
        index[top]["rows"].append(f)
    return templates.TemplateResponse(request, "config.html", _ctx(
        request, nav="config", grouped=grouped, version=cur["version"],
        updated_at=cur["updated_at"],
        raw=json.dumps(payload, ensure_ascii=False, indent=2),
        history=db.config_history(20), saved=saved, err=err, restored=restored,
        field_labels=managed.FIELD_LABELS,
        local_only=managed.LOCAL_ONLY_SECTIONS,
    ))


@router.post("/config")
async def config_save(request: Request):
    require_admin(request)
    form = await request.form()
    cur = db.config_current()
    base = cur["payload"] or json.loads(json.dumps(managed.DEFAULT_MANAGED_CONFIG))
    base = json.loads(json.dumps(base))

    raw = str(form.get("raw_json") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("顶层必须是对象")
            base = parsed
        except Exception as e:
            return RedirectResponse("/config?err=%s" % _q("原始 JSON 解析失败：%s" % e),
                                    status_code=303)

    kinds = {}
    for f in _flatten(json.loads(json.dumps(managed.DEFAULT_MANAGED_CONFIG))):
        kinds[f["path"]] = f["kind"]
    for f in _flatten(base):
        kinds.setdefault(f["path"], f["kind"])

    for path, kind in kinds.items():
        if kind == "bool":
            _set_path(base, path, form.get(path) is not None)
        else:
            if path in form:
                old = None
                for f in _flatten(base):
                    if f["path"] == path:
                        old = f["value"]
                        break
                _set_path(base, path, _coerce(old, form.get(path), kind))

    payload = managed.filter_payload(base)
    if not payload:
        return RedirectResponse("/config?err=%s" % _q("过滤后没有任何可下发的内容，已放弃保存"),
                                status_code=303)
    ver = db.config_save(payload, note=str(form.get("note") or ""),
                         by=current_user(request) or "?")
    db.event("info", "console", "配置已更新到版本 v%d" % ver)
    return RedirectResponse("/config?saved=1", status_code=303)


@router.post("/config/restore/{ver}")
def config_restore(request: Request, ver: int):
    require_admin(request)
    row = None
    with db.tx() as c:
        row = c.execute("SELECT payload_json FROM configs WHERE version=?", (ver,)).fetchone()
    if not row:
        return RedirectResponse("/config?err=%s" % _q("找不到版本 v%d" % ver), status_code=303)
    payload = managed.filter_payload(json.loads(row["payload_json"]))
    newv = db.config_save(payload, note="回滚自 v%d" % ver, by=current_user(request) or "?")
    db.event("info", "console", "配置回滚：v%d -> 新版本 v%d" % (ver, newv))
    return RedirectResponse("/config?restored=1", status_code=303)


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(str(s)[:300], safe="")


# ------------------------------------------------------------------ 待执行任务

@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, created: int = 0, canceled: int = 0, err: str = ""):
    require_admin(request)
    return templates.TemplateResponse(request, "jobs.html", _ctx(
        request, nav="jobs", jobs=db.request_list(limit=60),
        allow=settings.ALLOW_RUN_REQUESTS,
        created=created, canceled=canceled, err=err))


@router.post("/jobs")
async def job_create(request: Request):
    require_admin(request)
    if not settings.ALLOW_RUN_REQUESTS:
        return RedirectResponse("/jobs?err=%s" % _q("服务端已关闭「触发任务」功能"), status_code=303)
    form = await request.form()
    slot = str(form.get("slot") or "auto")
    only = str(form.get("only") or "").strip()
    dry = form.get("dry_run") is not None
    note = str(form.get("note") or "")
    if slot not in ("auto", "00:00", "12:00"):
        slot = "auto"
    rid = db.request_create(slot, only, dry, current_user(request) or "?", note)
    db.event("info", "console", "新建待执行任务 #%d（%s 档，范围=%s）"
             % (rid, slot, only or "按档位"))
    return RedirectResponse("/jobs?created=%d" % rid, status_code=303)


@router.post("/jobs/{req_id}/cancel")
def job_cancel(request: Request, req_id: int):
    require_admin(request)
    ok = db.request_cancel(req_id)
    db.event("info", "console", "取消待执行任务 #%d（%s）" % (req_id, "成功" if ok else "状态不允许"))
    return RedirectResponse("/jobs?canceled=%d" % (1 if ok else 0), status_code=303)


# ------------------------------------------------------------------ 设置

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, ok: str = "", err: str = "", token: str = ""):
    require_admin(request)
    return templates.TemplateResponse(request, "settings.html", _ctx(
        request, nav="settings", agent_hint=db.kv_get("agent_token_hint") or "未初始化",
        last_host=db.kv_get("last_agent_host") or "—",
        last_ver=db.kv_get("last_agent_version") or "—",
        data_dir=str(settings.DATA_DIR), db_path=str(settings.DB_PATH),
        max_upload_mb=settings.MAX_UPLOAD_BYTES / 1048576.0,
        keep_runs=settings.KEEP_RUNS, max_shots=settings.MAX_SHOTS_PER_RUN,
        new_token=token, ok=ok, err=err))


@router.post("/settings/rotate-token")
def rotate_token(request: Request):
    require_admin(request)
    tok = security.rotate_agent_token()
    db.event("warn", "console", "agent 令牌已轮换")
    return RedirectResponse("/settings?ok=%s&token=%s"
                            % (_q("令牌已轮换，请立刻更新脚本侧配置"), _q(tok)),
                            status_code=303)


@router.post("/settings/password")
async def change_password(request: Request):
    require_admin(request)
    form = await request.form()
    cur = str(form.get("current") or "")
    new1 = str(form.get("new1") or "")
    new2 = str(form.get("new2") or "")
    user = current_user(request) or "admin"
    if not security.check_admin(user, cur):
        return RedirectResponse("/settings?err=%s" % _q("当前口令不对"), status_code=303)
    if len(new1) < 8:
        return RedirectResponse("/settings?err=%s" % _q("新口令至少 8 位"), status_code=303)
    if new1 != new2:
        return RedirectResponse("/settings?err=%s" % _q("两次输入不一致"), status_code=303)
    security.set_admin_password(user, new1)
    db.event("warn", "console", "管理员口令已修改")
    return RedirectResponse("/settings?ok=%s" % _q("口令已更新"), status_code=303)


# ------------------------------------------------------------------ 事件 & JSON

@router.get("/events", response_class=HTMLResponse)
def events_page(request: Request):
    require_admin(request)
    return templates.TemplateResponse(request, "events.html", _ctx(request, nav="events", events=db.events_recent(200)))


@router.get("/api/summary")
def api_summary(request: Request):
    require_admin(request)
    stats = db.run_stats(days=7)
    last = stats.get("last")
    return JSONResponse({
        "total": stats["total"], "ok_runs": stats["ok_runs"], "bad_runs": stats["bad_runs"],
        "last": None if not last else {
            "id": last["id"], "started_at": last["started_at"], "slot": last["slot"],
            "all_ok": bool(last["all_ok"]), "n_ok": last["n_ok"],
            "n_fail": last["n_fail"], "n_skip": last["n_skip"],
        },
        "pending_jobs": len(db.request_pending(limit=99)),
        "config_version": db.config_current()["version"],
    })


@router.get("/healthz")
def healthz():
    return {"ok": True, "version": settings.APP_VERSION, "runs": db.run_count()}
