# -*- coding: utf-8 -*-
"""管理端：页面 + 表单动作。服务端渲染，没有前端构建步骤。"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, managed, plan, realtime, security, settings

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

router = APIRouter(tags=["console"])


# ------------------------------------------------------------------ 鉴权
#
# 多用户模型（2026-09-20 起）：
#   · 登录态存在 session 里：{"user": 登录名, "uid": 用户 id, "role": admin|user}
#   · require_user()  —— 任何登录用户都能过，返回当前用户字典
#   · require_admin() —— 只有管理员能过，普通用户被 303 踢回首页
#
# ★ 为什么 session 里要存 uid 而不是只存用户名：用户名是可以被管理员改的，
#   改完之后老会话里的名字就对不上任何人了（用户会莫名其妙掉线，或者更糟 ——
#   改名后恰好撞上另一个同名用户，拿到别人的数据）。uid 是自增主键，永不变。
#
# ★ **没有**「只看自己的」的第二种 admin 概念。过滤靠两个显式参数传递
#   （own_only / owner_id），不靠全局状态 —— 全局状态在并发请求下会串味。

def current_uid(request: Request) -> Optional[int]:
    v = request.session.get("uid")
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def current_user(request: Request) -> Optional[str]:
    return request.session.get("user")


def current_account(request: Request) -> Optional[Dict[str, Any]]:
    """取当前登录用户的完整信息（每次请求现查库）。

    ★ 刻意**不**把用户信息整个塞进 session：
      · 管理员停用/删除某个用户后，那个用户的会话必须立刻失效 —— 塞进 session
        的话得等 4 小时过期，等于停用形同虚设；
      · 角色（admin/user）也可能被改，同样要立刻生效。
      现查库一次约 0.1ms，这点成本换「权限变更立即生效」非常值。
    """
    uid = current_uid(request)
    if uid is None:
        return None
    u = db.user_get(uid)
    if not u or not u.get("enabled"):
        return None                      # 被停用/删掉 → 当作没登录
    return u


def require_user(request: Request) -> Dict[str, Any]:
    u = current_account(request)
    if not u:
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return u


def require_admin(request: Request) -> Dict[str, Any]:
    """管理员专用。普通用户访问管理页 → 303 回首页并带说明。"""
    u = require_user(request)
    if not u.get("is_admin"):
        raise HTTPException(status_code=303, headers={"Location": "/?denied=1"})
    return u


def is_admin(request: Request) -> bool:
    u = current_account(request)
    return bool(u and u.get("is_admin"))


def scope_of(request: Request) -> Dict[str, Any]:
    """把「该看谁的数据」算成两个参数，给 db 层那些 *owner_id/own_only* 用。

    管理员 → own_only=False（看全部）
    普通用户 → own_only=True + 自己的 uid

    单独抽出来是为了**不重复**地在几十个路由里手写这段判断 ——
    漏写一处的后果是越权（看到别人的数据），而漏写很难靠肉眼发现。
    """
    u = current_account(request) or {}
    if u.get("is_admin"):
        return {"owner_id": None, "own_only": False}
    return {"owner_id": int(u["id"]) if u.get("id") else None, "own_only": True}


def _nav_badge(request: Request) -> Dict[str, Any]:
    """左侧导航上的角标数字。普通用户只统计自己的，不然角标会指向看不到的内容。"""
    if not current_account(request):
        return {}
    sc = scope_of(request)
    d: Dict[str, Any] = dict(db.client_online_count())
    try:
        d["roles_attention"] = len(
            db.role_daily_overview(days=1, **sc).get("attention") or [])
    except Exception:
        d["roles_attention"] = 0
    return d


def _ctx(request: Request, **kw) -> Dict[str, Any]:
    u = current_account(request)
    base = {
        "request": request,
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "user": current_user(request),
        "me": u,
        "is_admin": bool(u and u.get("is_admin")),
        "task_meta": managed.TASK_META,
        "status_label": managed.STATUS_LABEL,
        # 在线探测相关：模板里到处要用，统一放这儿，免得每页都传一遍
        "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
        "offline_after": settings.CLIENT_OFFLINE_AFTER,
        "nav_badge": _nav_badge(request),
        # 角色任务模式：账户/角色页与角色执行页都要用
        "plan_modes": plan.MODE_LABEL,
        "plan_task_keys": plan.TASK_KEYS,
        "plan_slots": plan.SLOTS,
        "task_name": {t["key"]: t["name"] for t in managed.TASK_META},
        # 指令类型的中文名（设备页/账号页显示「这条指令是干什么的」）
        "probe_kinds": db.PROBE_KINDS,
        "device_kinds": db.DEVICE_KINDS,
        # 两个页面共用 _probe_box.html / _device_rows.html 时要有默认值：
        # 账号页不会传 running / devices，缺了默认值模板里 `running` 就是 Undefined，
        # `{% if running %}` 在 Jinja 里对 Undefined 判 False（刚好是对的），
        # 但 `running.why` 会抛错 —— 所以显式兜一个 None。
        "running": None,
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
    if current_account(request):
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
    u = security.check_user(user, pwd)
    if not u:
        security.login_record_fail(ip)
        db.event("warn", "console", "登录失败（ip=%s，用户=%s）" % (ip, user))
        return templates.TemplateResponse(request, 
            "login.html", _ctx(request, err="用户名或口令不对"), status_code=401)

    security.login_reset(ip)
    # ★ 三样都要写：user 给人看，uid 用于「改名后老会话依然认得人」，
    #   role 只是给模板做首屏判断（真正的权限每次都现查库，见 current_account）
    request.session["user"] = u["username"]
    request.session["uid"] = int(u["id"])
    request.session["role"] = u.get("role") or "user"
    db.user_touch_login(int(u["id"]), ip)
    db.event("info", "console", "登录成功（ip=%s，用户=%s）" % (ip, u["username"]))
    # 被重置过口令的用户，进来先逼他改掉
    if u.get("must_change"):
        return RedirectResponse("/password?first=1", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ 我的口令

def _apply_password_change(u: Dict[str, Any], form, back: str) -> RedirectResponse:
    """改口令的公共逻辑 —— `/password` 与 `/settings/password` 共用。

    ★ 抽出来是为了**两处校验不漂移**：之前 `settings/password` 只查了长度 ≥ 8，
      没走 `password_problem()`，于是「管理员可以在设置页把口令改成 12345678」
      而普通用户在 /password 页却改不了 —— 同一条规则分两处实现必然分叉。

    `first=True` 时跳过「验证当前口令」：那是管理员刚发的一次性口令，
    输它一次没有意义（用户本来就是为了换掉它才来的）。
    """
    cur = str(form.get("current") or "")
    new1 = str(form.get("new1") or "")
    new2 = str(form.get("new2") or "")
    first = bool(form.get("first"))
    sep = "&" if "?" in back else "?"

    if not first:
        row = db.user_by_name(u["username"], with_hash=True) or {}
        if not security.verify_secret(cur, row.get("pwd_hash") or ""):
            return RedirectResponse("%s%serr=%s" % (back, sep, _q("当前口令不对")),
                                    status_code=303)
    if new1 != new2:
        return RedirectResponse("%s%serr=%s" % (back, sep, _q("两次输入不一致")),
                                status_code=303)
    problem = security.password_problem(new1, u["username"])
    if problem:
        return RedirectResponse("%s%serr=%s" % (back, sep, _q(problem)),
                                status_code=303)

    security.set_user_password(int(u["id"]), new1)
    db.event("info", "console", "用户「%s」修改了自己的口令" % u["username"])
    return RedirectResponse("%s%sok=%s" % (back, sep, _q("口令已更新")),
                            status_code=303)


@router.get("/password", response_class=HTMLResponse)
def password_page(request: Request, first: int = 0, ok: str = "", err: str = ""):
    u = require_user(request)
    return templates.TemplateResponse(request, "password.html", _ctx(
        request, nav="password", first=bool(first), ok=ok, err=err, me=u))


@router.post("/password")
async def password_change(request: Request):
    """改自己的口令。

    ★ 与 /settings/password 的区别：这一页**任何登录用户**都能用（改自己的），
      那一页只改管理员的。普通用户没有 /settings 的权限，所以必须有这一页，
      否则被管理员重置口令后（must_change=1）他就被永久锁死在改密提示上了。
    """
    u = require_user(request)
    form = await request.form()
    back = "/password?first=1" if form.get("first") else "/password"
    return _apply_password_change(u, form, back)


# ------------------------------------------------------------------ 仪表盘

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, denied: int = 0):
    u = require_user(request)
    sc = scope_of(request)
    stats = db.run_stats(days=14, **sc)
    runs = db.run_list(limit=8, **sc)
    if sc["own_only"]:
        pending = db.request_list(limit=5, owner_id=sc["owner_id"], own_only=True)
    else:
        pending = db.request_pending(limit=5)
    cur = db.config_current()
    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        request, nav="dash", stats=stats, runs=runs, pending=pending,
        config_version=cur["version"],
        config_updated=cur["updated_at"],
        agent_hint=db.kv_get("agent_token_hint") or "未初始化",
        last_agent_host=db.kv_get("last_agent_host") or "—",
        clients=db.client_list(),
        online=db.client_online_count(),
        my_accounts=db.account_list(with_roles=False, **sc),
        tenant=sc["own_only"],
        denied=bool(denied),
    ))


# ------------------------------------------------------------------ 运行记录

@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, page: int = 1, bad: int = 0, deleted: int = 0,
              client: str = "", account: str = ""):
    require_user(request)
    sc = scope_of(request)
    page = max(1, page)
    per = 25
    cid = int(client) if str(client).isdigit() else None
    aid = int(account) if str(account).isdigit() else None
    # 普通用户指定了别人的账号 id 也不能看 —— 改写成自己的账号列表之外就无结果
    if aid is not None and sc["own_only"] and not db.account_owned_by(aid, sc["owner_id"]):
        aid = -1                      # 一个不存在的 id，查出来必然是空
    rows = db.run_list(limit=per, offset=(page - 1) * per, only_failed=bool(bad),
                       client_id=cid, account_id=aid, **sc)
    return templates.TemplateResponse(request, "runs.html", _ctx(
        request, nav="runs", runs=rows, page=page, per=per,
        total=db.run_count(only_failed=bool(bad), client_id=cid, account_id=aid, **sc),
        bad=bool(bad), deleted=deleted, client=str(client or ""),
        account=str(account or ""), client_id=cid, account_id=aid,
        clients=db.client_list(),
        accounts=db.account_list(with_roles=False, **sc)))


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    require_user(request)
    sc = scope_of(request)
    row = db.run_get(run_id)
    # 不存在与「不是你的」返回同一个 404 —— 不给普通用户任何「这条记录存在」
    # 的旁证（否则能拿它枚举出别人跑过多少轮）
    if not row or not db.run_owned_by(run_id, sc["owner_id"]):
        raise HTTPException(status_code=404, detail="没有这条运行记录")
    # run_get 返回的是单行、没有 join 客户端名，这里补一个展示用的副本
    display = dict(row)
    if display.get("client_id"):
        cli = db.client_get(int(display["client_id"]))
        display["client_name"] = (cli or {}).get("name") or (cli or {}).get("host")
    else:
        display["client_name"] = None
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
        request, nav="runs", run=display, tasks=tasks, report=report, orphan=orphan,
        env=json.loads(row["env_json"] or "{}"),
        notes=json.loads(row["notes_json"] or "[]"),
        total_shots=sum(len(v) for v in shots_by_key.values()),
    ))


@router.post("/runs/{run_id}/delete")
def run_delete(request: Request, run_id: int):
    require_user(request)
    sc = scope_of(request)
    if not db.run_owned_by(run_id, sc["owner_id"]):
        return RedirectResponse("/runs?err=%s" % _q("没有这条运行记录"), status_code=303)
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
    require_user(request)
    sc = scope_of(request)
    if not db.run_owned_by(run_id, sc["owner_id"]):
        raise HTTPException(status_code=404, detail="这条运行没有报告")
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
    require_user(request)
    sc = scope_of(request)
    art = db.artifact_get(aid)
    if not art or not db.run_owned_by(int(art["run_id"]), sc["owner_id"]):
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
    # 配置变了 → 通知所有在线客户端「你手里的版本过期了」，它们会自行决定何时重拉
    pushed = realtime.notify_all("config", version=ver)
    db.event("info", "console", "配置已更新到版本 v%d%s"
             % (ver, "（已实时通知 %d 台客户端）" % pushed if pushed else ""))
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
    realtime.notify_all("config", version=newv)
    db.event("info", "console", "配置回滚：v%d -> 新版本 v%d" % (ver, newv))
    return RedirectResponse("/config?restored=1", status_code=303)


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(str(s)[:300], safe="")


# ------------------------------------------------------------------ 待执行任务

@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, created: int = 0, canceled: int = 0, err: str = ""):
    u = require_user(request)
    sc = scope_of(request)
    return templates.TemplateResponse(request, "jobs.html", _ctx(
        request, nav="jobs",
        jobs=db.request_list(limit=60, owner_id=sc["owner_id"], own_only=sc["own_only"]),
        allow=settings.ALLOW_RUN_REQUESTS,
        clients=db.client_list(),
        # ★ 可派任务的设备清单（2026-09-21）。之前这里只有「客户端」下拉，
        #   于是「用手机跑还是用模拟器跑」在页面上根本选不出来 —— 见 db 里
        #   run_requests.serial 的注释。
        devices=db.device_list(),
        my_accounts=db.account_list(with_roles=False, **sc),
        created=created, canceled=canceled, err=err,
        is_admin=bool(u.get("is_admin"))))


@router.post("/jobs")
async def job_create(request: Request):
    u = require_user(request)
    sc = scope_of(request)
    if not settings.ALLOW_RUN_REQUESTS:
        return RedirectResponse("/jobs?err=%s" % _q("服务端已关闭「触发任务」功能"), status_code=303)
    form = await request.form()
    slot = str(form.get("slot") or "auto")
    only = str(form.get("only") or "").strip()
    dry = form.get("dry_run") is not None
    note = str(form.get("note") or "")
    raw_cid = str(form.get("client_id") or "").strip()
    cid = int(raw_cid) if raw_cid.isdigit() else None
    if cid is not None and not db.client_get(cid):
        return RedirectResponse("/jobs?err=%s" % _q("指定的客户端不存在"), status_code=303)

    # ★ 目标设备（2026-09-21 加）。指定了设备就**同时锁定它所属的那台客户端** ——
    #   设备是挂在一台电脑上的，派给别的电脑等于让它去连一台它没有的设备。
    #   这一步不能省：只写 device_id 不写 client_id 的话，别的客户端会把任务领走、
    #   然后连不上任何设备，报一个和真实原因无关的错。
    did = None
    dev_serial = None
    raw_did = str(form.get("device_id") or "").strip()
    if raw_did.isdigit():
        dev = db.device_get(int(raw_did))
        if not dev:
            return RedirectResponse("/jobs?err=%s" % _q("指定的设备不存在或已被删除"),
                                    status_code=303)
        if not dev.get("enabled"):
            return RedirectResponse(
                "/jobs?err=%s" % _q("设备「%s」已被停用，先启用再派任务"
                                    % (dev.get("name") or dev.get("serial"))),
                status_code=303)
        s = str(dev.get("serial") or "").strip()
        if not s:
            return RedirectResponse("/jobs?err=%s" % _q("这台设备没有 serial，无法派任务"),
                                    status_code=303)
        did, dev_serial = int(dev["id"]), s
        owner_cid = dev.get("client_id")
        if owner_cid:
            cid = int(owner_cid)          # 设备在哪台电脑上，就派给哪台
        elif cid is None:
            return RedirectResponse(
                "/jobs?err=%s" % _q("设备「%s」还没挂到任何客户端上"
                                    "（先在这台机器上跑 agent.py）"
                                    % (dev.get("name") or s)), status_code=303)

    if slot not in ("auto", "00:00", "12:00"):
        slot = "auto"
    # 归属：普通用户排的队挂在自己名下（他能在列表里看到并取消）；
    # 管理员排的是公共的（owner_id=None），普通用户也能看到 —— 见 db.request_list。
    owner = None if u.get("is_admin") else int(u["id"])
    rid = db.request_create(slot, only, dry, u["username"], note,
                            client_id=cid, owner_id=owner,
                            device_id=did, serial=dev_serial)
    # ★ 立刻推给采集端，别让它等到下一拍心跳（最多 30 秒）才知道有活干。
    #   推送只是「提醒」：任务本身已经落库，推不到也会被心跳兜住。
    #   定向任务只推给点名的那台；公共任务谁都能领，所以推给全部在线客户端。
    if cid:
        realtime.notify_client(cid, "jobs", job=rid)
    else:
        realtime.notify_all("jobs", job=rid)
    target = (db.client_get(cid) or {}).get("name") if cid else "任意客户端"
    on_dev = "，设备=%s" % dev_serial if dev_serial else ""
    db.event("info", "console", "新建待执行任务 #%d（%s 档，范围=%s，执行者=%s%s，排队人=%s）"
             % (rid, slot, only or "按档位", target, on_dev, u["username"]))
    return RedirectResponse("/jobs?created=%d" % rid, status_code=303)


@router.post("/jobs/{req_id}/cancel")
def job_cancel(request: Request, req_id: int):
    require_user(request)
    sc = scope_of(request)
    row = None
    with db.tx() as c:
        row = c.execute("SELECT owner_id,status FROM run_requests WHERE id=?",
                        (req_id,)).fetchone()
    if not row:
        return RedirectResponse("/jobs?err=%s" % _q("没有这条待执行任务"), status_code=303)
    # 普通用户只能取消自己排的；管理员排的公共任务只有管理员能取消
    if sc["own_only"]:
        o = row["owner_id"]
        if o is None or int(o) != int(sc["owner_id"]):
            return RedirectResponse("/jobs?err=%s" % _q("只能取消自己排的任务"), status_code=303)
    ok = db.request_cancel(req_id)
    db.event("info", "console", "取消待执行任务 #%d（%s）"
             % (req_id, "成功" if ok else "状态不允许"))
    return RedirectResponse("/jobs?canceled=%d" % (1 if ok else 0), status_code=303)


# ================================================================== 客户端（在线探测）
#
# 拓扑：后端与采集端可能不在同一台机器（也可能就在同一台），但后端一律
# **不主动连**采集端 —— 所有交互都由采集端发起。
# 采集端常驻时会建一条 WebSocket 长连接（见 realtime.py）：后台派任务 / 下指令
# **毫秒级**送达；长连接没建起来（防火墙、反代没透传 Upgrade、NAT 掐空闲连接……）
# 就退回心跳节奏。
#
# 两条铁律：
#   ①「在线 / 离线」始终看心跳的 last_seen 年龄 —— **不能**拿长连接在不在当判据
#      （长连接只影响快慢，不影响在不在）。
#   ② 待办状态始终在数据库里，推送只是「喊一声」；所以断开最多让事情慢一拍，绝不丢活。

@router.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, ok: str = "", err: str = "", hl: int = 0):
    """客户端页 —— **只读对普通用户开放**。

    普通用户需要看「机器在不在、跑的是哪个角色」（这决定他的任务什么时候能轮到），
    所以列表给他看。但改名 / 指派 / 暂停 / 探测 / 删除全归管理员 ——
    那些是共用主机的调度权，放开等于谁都能把别人的任务掐掉。
    """
    u = require_user(request)
    sc = scope_of(request)
    rows = db.client_list()
    accounts = db.account_list(**sc)
    # 客户端行的账号下拉：只显示启用的账号，禁用的一律不出现在选择里
    for c in rows:
        c["_roles"] = [r for r in (db.role_list(int(c["account_id"]))
                                   if c.get("account_id") else []) if r.get("enabled")]
        # 实时通道此刻连没连上。**只作提示**（连上=推送毫秒到，没连=等下一拍心跳），
        # 「在线/离线」始终看 age_seconds —— 别拿实时通道当在线判据。
        c["realtime"] = realtime.is_connected(c.get("uid") or "")
    return templates.TemplateResponse(request, "clients.html", _ctx(
        request, nav="clients", clients=rows, accounts=accounts,
        online=db.client_online_count(), ok=ok, err=err, hl=hl,
        role_list=db.role_list(**sc), read_only=not u.get("is_admin"),
    ))


@router.post("/clients/{cid}/rename")
async def client_rename(request: Request, cid: int):
    require_admin(request)
    form = await request.form()
    name = str(form.get("name") or "").strip()[:40]
    note = str(form.get("note") or "").strip()[:200]
    db.client_update(cid, name=name or None, note=note)
    db.event("info", "console", "客户端 #%d 改名为「%s」" % (cid, name or "（空）"))
    return RedirectResponse("/clients?hl=%d" % cid, status_code=303)


@router.post("/clients/{cid}/assign")
async def client_assign(request: Request, cid: int):
    """指派某台客户端该跑哪个账号 / 哪个角色。

    这一步是「后端设置账号角色、客户端自动切换」的入口：改完这里，
    客户端连着实时通道就**立刻**看到 switch_needed=true 并切过去；
    没连上则下一次心跳看到，然后自己切。两条路径取的是同一份状态（build_client_state）。
    """
    require_admin(request)
    form = await request.form()
    raw_a = str(form.get("account_id") or "").strip()
    raw_r = str(form.get("role_id") or "").strip()
    aid = int(raw_a) if raw_a.isdigit() else None
    rid = int(raw_r) if raw_r.isdigit() else None

    if aid is None:
        db.client_update(cid, account_id=None, role_id=None)
        realtime.push_state_to_client(cid)
        db.event("info", "console", "客户端 #%d 取消账号指派" % cid)
        return RedirectResponse("/clients?hl=%d" % cid, status_code=303)

    acc = db.account_get(aid)
    if not acc:
        return RedirectResponse("/clients?err=%s" % _q("账号不存在"), status_code=303)
    # 角色必须属于这个账号，否则界面被绕过时会写出脏数据
    if rid is not None:
        role = db.role_get(rid)
        if not role or int(role["account_id"]) != aid:
            return RedirectResponse("/clients?err=%s" % _q("角色不属于该账号，已忽略角色"),
                                    status_code=303)
    db.client_update(cid, account_id=aid, role_id=rid)
    realtime.push_state_to_client(cid)
    label = "%s / %s" % (acc["label"], (db.role_get(rid) or {}).get("name") or "不限角色") \
        if rid else acc["label"]
    db.event("info", "console", "客户端 #%d 指派为 %s" % (cid, label))
    return RedirectResponse("/clients?hl=%d&ok=%s" % (cid, _q("已指派：%s" % label)),
                            status_code=303)


@router.post("/clients/{cid}/toggle")
def client_toggle(request: Request, cid: int):
    """暂停 / 恢复某台客户端的任务派发。暂停后它领不到任务，但心跳照常。"""
    require_admin(request)
    cli = db.client_get(cid)
    if not cli:
        return RedirectResponse("/clients?err=%s" % _q("客户端不存在"), status_code=303)
    newv = 0 if cli.get("enabled") else 1
    db.client_update(cid, enabled=newv)
    # 推一份新状态过去，客户端立刻知道「我被暂停了 / 恢复了」，不用等下一拍心跳
    realtime.push_state_to_client(cid)
    db.event("warn", "console", "客户端 #%d %s任务派发" % (cid, "恢复" if newv else "暂停"))
    return RedirectResponse("/clients?hl=%d&ok=%s"
                            % (cid, _q("已%s任务派发" % ("恢复" if newv else "暂停"))),
                            status_code=303)


@router.post("/clients/{cid}/probe")
async def client_probe(request: Request, cid: int):
    """给客户端挂一条一次性指令。连着实时通道就**秒到**，否则下次心跳取回。"""
    require_admin(request)
    form = await request.form()
    kind = str(form.get("kind") or "probe")
    note = str(form.get("note") or "").strip()[:120]
    if not db.client_get(cid):
        return RedirectResponse("/clients?err=%s" % _q("客户端不存在"), status_code=303)
    p = db.client_request_probe(cid, kind=kind, by=current_user(request) or "?", note=note)
    # 指令已落库；推进去只是让它别等（客户端收到后仍走 /ping 那条路取指令）
    pushed = realtime.notify_client(cid, "command", probe=p.get("id"))
    db.event("info", "console", "请求客户端 #%d 执行「%s」%s"
             % (cid, db.PROBE_KINDS.get(kind, kind), "（已实时推送）" if pushed else ""))
    return RedirectResponse("/clients?hl=%d&ok=%s"
                            % (cid, _q("已下发指令%s"
                                       % ("（实时通道已送达）" if pushed
                                          else "，等它下次心跳（最多 %d 秒）"
                                               % settings.HEARTBEAT_INTERVAL))),
                            status_code=303)


@router.post("/clients/{cid}/probe/clear")
def client_probe_clear(request: Request, cid: int):
    require_admin(request)
    db.client_set_probe(cid, None)
    return RedirectResponse("/clients?hl=%d" % cid, status_code=303)


@router.post("/clients/{cid}/delete")
def client_delete(request: Request, cid: int):
    require_admin(request)
    if db.client_delete(cid):
        db.event("warn", "console", "删除客户端 #%d" % cid)
    return RedirectResponse("/clients?ok=%s" % _q("已删除该客户端记录"), status_code=303)


@router.get("/api/clients")
def api_clients(request: Request):
    """给页面自动刷新用：只返回在线状态，轻量。

    普通用户也能读 —— 客户端页对他们只读开放，这个接口就是给那页刷新的。
    返回的字段里**不含**账号/角色指派（那是管理信息），只有在线状态。
    """
    require_user(request)
    rows = db.client_list()
    return JSONResponse({
        "online": db.client_online_count(),
        "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
        "offline_after": settings.CLIENT_OFFLINE_AFTER,
        "clients": [{
            "id": c["id"], "name": c.get("name"), "host": c.get("host"),
            "online": c["online"], "age_seconds": c["age_seconds"],
            "last_seen": c.get("last_seen"), "state": (c.get("status") or {}).get("state"),
            "note": (c.get("status") or {}).get("note"),
            "current": (c.get("status") or {}).get("current") or {},
            "enabled": bool(c.get("enabled")),
            "probe_status": (c.get("probe") or {}).get("status"),
            # 实时通道是否连着。同样只作提示：连上=后台的指令/任务秒到，
            # 没连=退回心跳节奏（最多等一个心跳间隔）。不是在线判据。
            "realtime": realtime.is_connected(c.get("uid") or ""),
        } for c in rows],
    })


# ================================================================== 游戏账号 / 角色

@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, ok: str = "", err: str = "", hl: int = 0):
    require_user(request)
    sc = scope_of(request)
    accounts = db.account_list(**sc)
    # 角色的「执行任务模式」在模板里要直接渲染，这里先解析好
    for a in accounts:
        for r in a["roles"]:
            r["plan"] = plan.loads_plan(r.get("task_plan"))
            r["plan_text"] = plan.summary_text(r.get("task_plan"))
            # ★ 角色自动发现的痕迹：在游戏里找不到的角色要显眼地标出来，
            #   让用户判断是改名了还是真没了（系统只标不删，见 db.role_sync_discovered）
            r["_stale"] = bool(r.get("missing_at")) and \
                str(r.get("missing_at") or "") > str(r.get("seen_at") or "")
    # 每个账号下挂了几台客户端 / 几台设备（用于提示「删了会影响谁」）
    users: Dict[int, List[Any]] = {}
    for c in db.client_list():
        if c.get("account_id"):
            users.setdefault(int(c["account_id"]), []).append(c)
    holders: Dict[int, List[Any]] = {}
    for d in db.device_list():
        if d.get("account_id"):
            holders.setdefault(int(d["account_id"]), []).append(d)
    for a in accounts:
        a["_holders"] = holders.get(int(a["id"]), [])
    return templates.TemplateResponse(request, "accounts.html", _ctx(
        request, nav="accounts", accounts=accounts, ok=ok, err=err, hl=hl,
        users=users, clients=db.client_list(),
        # 管理员才能在「新增/编辑账号」里改归属；普通用户建号恒归自己，不需要这个下拉
        all_users=db.user_list() if is_admin(request) else [],
        # 模板里「客户端指派一览」要按 role_id 反查角色名，这里必须传（原来漏了）
        role_list=db.role_list(**sc),
        # 角色发现要选一台客户端来执行；结果也会落到设备页，普通用户看不到那边
        devices_all=db.device_list() if is_admin(request) else [],
        running=_anyone_running() if is_admin(request) else None))


def _plan_from_form(form) -> str:
    """从表单里读「执行任务模式」，整成 JSON 文本。

    表单里没有 `plan_mode` 这个字段时返回空串 —— 表示「本次不涉及任务模式」，
    调用方要据此**保持原值**而不是覆盖成默认值。
    （新增角色表单里就没有这一项，只有专门的配置表单才有。）
    """
    if "plan_mode" not in form:
        return ""
    mode = str(form.get("plan_mode") or plan.MODE_INHERIT)
    tasks = {k: (form.get("task_%s" % k) is not None) for k in plan.TASK_KEYS}
    slots = [s for s, f in (("00:00", "slot_00"), ("12:00", "slot_12"))
             if form.get(f) is not None]
    return plan.dumps_plan({"mode": mode, "tasks": tasks, "slots": slots,
                            "note": str(form.get("plan_note") or "")[:200]})


@router.post("/accounts")
async def account_create(request: Request):
    u = require_user(request)
    form = await request.form()
    label = str(form.get("label") or "").strip()
    if not label:
        return RedirectResponse("/accounts?err=%s" % _q("账号名不能为空"), status_code=303)
    # 归属：普通用户建的账号归他自己；管理员可以显式指定归属（用于代建），
    # 不指定则为公共（owner_id=None）。
    # ★ 只有管理员能读 owner_id 这个表单字段 —— 否则普通用户 POST 一个
    #   owner_id=别人 就能把账号塞进别人名下（或更糟：改成 None 变成公共账号）。
    owner = None if u.get("is_admin") else int(u["id"])
    if u.get("is_admin") and "owner_id" in form:
        raw = str(form.get("owner_id") or "").strip()
        owner = int(raw) if raw.isdigit() and db.user_get(int(raw)) else None
    aid = db.account_create(label,
                            login_name=str(form.get("login_name") or ""),
                            masked=str(form.get("masked") or ""),
                            tag=str(form.get("tag") or ""),
                            note=str(form.get("note") or ""),
                            owner_id=owner,
                            password=str(form.get("password") or ""),
                            pwd_hint=str(form.get("pwd_hint") or ""))
    db.event("info", "console", "新增游戏账号「%s」（#%d，归属=%s%s）"
             % (label, aid, u["username"] if owner else "管理员/公共",
                "，已设密码" if str(form.get("password") or "").strip() else ""))
    # ★ 用户需求：「添加完之后后台会自动查看目前有没有在跑任务，如果没有跑会提示
    #   可以添加然后启动模拟器或者实体手机配合用户完成添加」。
    #   这里不自动下发任何指令（自动启模拟器会打断别人正在跑的任务），
    #   只把「现在空闲/在忙」如实带回去，让用户在页面上自己决定下一步。
    busy = _anyone_running()
    if busy:
        return RedirectResponse(
            "/accounts?hl=%d&ok=%s" % (aid, _q(
                "账号「%s」已添加。注意：%s —— 要连设备登录请等它跑完，"
                "否则会打断正在跑的任务" % (label, busy["why"]))),
            status_code=303)
    return RedirectResponse(
        "/accounts?hl=%d&ok=%s#a%d" % (aid, _q(
            "账号「%s」已添加。现在没有任务在跑 —— 可以到「设备管理」"
            "启动模拟器 / 添加实体手机，再点「发现角色」把游戏里的角色读回来"
            % label), aid),
        status_code=303)


@router.post("/accounts/{aid}/update")
async def account_update(request: Request, aid: int):
    u = require_user(request)
    sc = scope_of(request)
    if not db.account_owned_by(aid, sc["owner_id"]):
        return RedirectResponse("/accounts?err=%s" % _q("没有这个账号"), status_code=303)
    form = await request.form()
    fields: Dict[str, Any] = {}
    for k in ("label", "login_name", "masked", "tag", "note"):
        if k in form:
            fields[k] = str(form.get(k) or "").strip()[:400]
    if "enabled" in form or form.get("_has_enabled"):
        fields["enabled"] = 1 if form.get("enabled") is not None else 0
    # 改归属只归管理员（同上：普通用户能改归属 = 能把账号甩给别人或变成公共）
    if u.get("is_admin") and "owner_id" in form:
        raw = str(form.get("owner_id") or "").strip()
        fields["owner_id"] = int(raw) if raw.isdigit() and db.user_get(int(raw)) else None
    if not str(fields.get("label") or "").strip():
        fields.pop("label", None)
    db.account_update(aid, **fields)
    db.event("info", "console", "更新游戏账号 #%d" % aid)
    return RedirectResponse("/accounts?hl=%d&ok=%s" % (aid, _q("已保存")), status_code=303)


@router.post("/accounts/{aid}/password")
async def account_set_password(request: Request, aid: int):
    """给游戏账号设 / 改 / 清密码（用户需求：「后台新增账号要带密码」）。

    ★ 存的是 scrypt 哈希（`db.account_set_password` → `security.hash_secret`），
      **不是明文**，且哈希永远不出数据层（`_scrub_account` 把它换成 has_password）。
      所以这里的语义是「凭据台账」：账号掉了要人工重登时能核对密码对不对，
      **不能**用它自动登录网易 —— 登录走的是游戏的「常用账号列表」（免密）。
      这一点在 accounts.html 的说明块里也写了，免得用户以为填了密码就能自动登录。

    `pwd_hint` 是**明文提示**（用户自己写的，比如「尾号4508那个」），
    和密码本身无关，可以放心显示在界面上。
    """
    require_user(request)
    sc = scope_of(request)
    if not db.account_owned_by(aid, sc["owner_id"]):
        return RedirectResponse("/accounts?err=%s" % _q("没有这个账号"), status_code=303)
    if not db.account_get(aid):
        return RedirectResponse("/accounts?err=%s" % _q("账号不存在"), status_code=303)
    form = await request.form()
    pwd = str(form.get("password") or "").strip()
    hint = str(form.get("pwd_hint") or "").strip()
    if form.get("clear"):
        db.account_set_password(aid, "", hint)
        db.event("warn", "console", "清空游戏账号 #%d 的密码" % aid)
        return RedirectResponse("/accounts?hl=%d&ok=%s" % (aid, _q("已清空该账号的密码")),
                                status_code=303)
    if not pwd:
        return RedirectResponse("/accounts?hl=%d&err=%s" % (aid, _q("密码不能为空（要清空请点「清空密码」）")),
                                status_code=303)
    if len(pwd) < 6:
        # 游戏账号密码是网易那边的规则，这里只做个下限提醒 —— 不替对方定规则，
        # 但太短的「凭据」记了也没意义（真要核对时自己都不确定打对了没）。
        return RedirectResponse("/accounts?hl=%d&err=%s" % (aid, _q("密码至少 6 位")),
                                status_code=303)
    db.account_set_password(aid, pwd, hint)
    db.event("info", "console", "设置游戏账号 #%d 的密码（哈希入库）" % aid)
    return RedirectResponse("/accounts?hl=%d&ok=%s" % (aid, _q("密码已保存（只存哈希，无法回读）")),
                            status_code=303)


@router.post("/accounts/{aid}/delete")
def account_delete(request: Request, aid: int):
    require_user(request)
    sc = scope_of(request)
    if not db.account_owned_by(aid, sc["owner_id"]):
        return RedirectResponse("/accounts?err=%s" % _q("没有这个账号"), status_code=303)
    acc = db.account_get(aid)
    db.account_delete(aid)
    db.event("warn", "console", "删除游戏账号 #%d（%s）"
             % (aid, (acc or {}).get("label") or "?"))
    return RedirectResponse("/accounts?ok=%s" % _q("账号及其角色已删除，相关客户端指派已清空"),
                            status_code=303)


@router.post("/accounts/{aid}/roles")
async def role_create(request: Request, aid: int):
    require_user(request)
    sc = scope_of(request)
    if not db.account_owned_by(aid, sc["owner_id"]):
        return RedirectResponse("/accounts?err=%s" % _q("没有这个账号"), status_code=303)
    if not db.account_get(aid):
        return RedirectResponse("/accounts?err=%s" % _q("账号不存在"), status_code=303)
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/accounts?hl=%d&err=%s" % (aid, _q("角色名不能为空")),
                                status_code=303)
    rid = db.role_create(aid, name,
                         server=str(form.get("server") or ""),
                         season=str(form.get("season") or ""),
                         tab=str(form.get("tab") or ""),
                         note=str(form.get("note") or ""),
                         task_plan=_plan_from_form(form))
    db.event("info", "console", "账号 #%d 新增角色「%s」（#%d）" % (aid, name, rid))
    return RedirectResponse("/accounts?hl=%d&ok=%s" % (aid, _q("角色已添加")), status_code=303)


@router.post("/roles/{rid}/update")
async def role_edit(request: Request, rid: int):
    require_user(request)
    sc = scope_of(request)
    if not db.role_owned_by(rid, sc["owner_id"]):
        return RedirectResponse("/accounts?err=%s" % _q("没有这个角色"), status_code=303)
    role = db.role_get(rid)
    if not role:
        return RedirectResponse("/accounts?err=%s" % _q("角色不存在"), status_code=303)
    form = await request.form()
    fields: Dict[str, Any] = {}
    for k in ("name", "server", "season", "tab", "note"):
        if k in form:
            fields[k] = str(form.get(k) or "").strip()[:400]
    if form.get("_has_enabled"):
        fields["enabled"] = 1 if form.get("enabled") is not None else 0
    if not str(fields.get("name") or "").strip():
        fields.pop("name", None)
    tp = _plan_from_form(form)
    if tp:
        fields["task_plan"] = tp
    db.role_update(rid, **fields)
    db.event("info", "console", "更新角色 #%d" % rid)
    return RedirectResponse("/accounts?hl=%d&ok=%s"
                            % (int(role["account_id"]), _q("角色已保存")), status_code=303)


@router.post("/roles/{rid}/save")
async def role_save(request: Request, rid: int):
    """保存单个角色的全部设置（在「角色执行」页上配置）。

    这是「每个游戏角色跑什么任务」的唯一入口，保存后不需要动客户端配置 ——
    客户端连着实时通道立刻拿到新的 task_plan，没连上则下一次心跳拿到。

    字段：name / tab / server / season（备注）/ enabled / mode / task_* / slot_* / note
    """
    require_user(request)
    sc = scope_of(request)
    if not db.role_owned_by(rid, sc["owner_id"]):
        return RedirectResponse("/roles?err=%s" % _q("没有这个角色"), status_code=303)
    role = db.role_get(rid)
    if not role:
        return RedirectResponse("/roles?err=%s" % _q("角色不存在"), status_code=303)
    form = await request.form()

    fields: Dict[str, Any] = {}
    for k in ("name", "server", "season", "tab"):
        if k in form:
            fields[k] = str(form.get(k) or "").strip()[:60]
    if not str(fields.get("name") or "").strip():
        fields.pop("name", None)        # 名字空着就保持原值，绝不写空
    # 有 name 输入框但为空 → 上面已剔除；这里处理启停
    if form.get("_has_enabled") or "enabled" in form:
        fields["enabled"] = 1 if form.get("enabled") is not None else 0

    tp = _plan_from_form(form)
    if tp:
        fields["task_plan"] = tp
    db.role_update(rid, **fields)
    # 任务模式/启停变了，立刻推给正跑着这个角色的客户端（推不到的会被心跳兜住）
    realtime.push_state_for_role(rid, account_id=role.get("account_id"))

    p = plan.loads_plan(tp or role.get("task_plan"))
    if p["mode"] == plan.MODE_CUSTOM and not plan.selected_tasks(p):
        db.event("warn", "console",
                 "角色「%s」被设为「自定义」，但一个任务都没勾 —— 它不会执行任何任务"
                 % role.get("name"))
        return RedirectResponse(
            "/roles?hl=%d#r%d&warn=%s" % (rid, rid, _q(
                "已保存，但注意：「自定义」模式下没勾选任何任务，这个角色不会执行任何任务")),
            status_code=303)
    db.event("info", "console", "角色「%s」已保存：%s"
             % (fields.get("name") or role.get("name"), plan.summary_text(tp or role.get("task_plan"))))
    return RedirectResponse("/roles?hl=%d#r%d&ok=%s"
                            % (rid, rid, _q("「%s」已保存：%s"
                                            % (fields.get("name") or role.get("name"),
                                               plan.summary_text(tp or role.get("task_plan"))))),
                            status_code=303)


@router.post("/roles/{rid}/delete")
def role_delete(request: Request, rid: int):
    require_user(request)
    sc = scope_of(request)
    if not db.role_owned_by(rid, sc["owner_id"]):
        return RedirectResponse("/accounts?err=%s" % _q("没有这个角色"), status_code=303)
    role = db.role_get(rid)
    if not role:
        return RedirectResponse("/accounts?err=%s" % _q("角色不存在"), status_code=303)
    aid = int(role["account_id"])
    db.role_delete(rid)
    db.event("warn", "console", "删除角色 #%d（%s）" % (rid, role.get("name") or "?"))
    return RedirectResponse("/accounts?hl=%d&ok=%s" % (aid, _q("角色已删除")), status_code=303)


# ================================================================== 角色执行情况
#
# 用户最关心的两件事，都收敛在这一页：
#   1. **每个角色跑什么任务**（执行任务模式）—— 直接在这里勾选，保存即下发
#   2. **每个角色每天跑得怎么样** —— 按天、按档位、按任务列出结果
#
# 数据来源：runs 表按角色聚合（db.role_daily_overview）。
# 归属优先看服务端指派，没指派时按客户端自报的角色名匹配 —— 见 db.role_find_by_name。

@router.get("/roles", response_class=HTMLResponse)
def roles_page(request: Request, days: int = 7, hl: int = 0,
               ok: str = "", err: str = "", warn: str = ""):
    require_user(request)
    sc = scope_of(request)
    days = max(1, min(int(days or 7), 30))
    ov = db.role_daily_overview(days=days, **sc)
    return templates.TemplateResponse(request, "roles.html", _ctx(
        request, nav="roles", ov=ov, days=days, hl=hl,
        ok=ok, err=err, warn=warn, clients=db.client_list()))


# ================================================================== 设备管理
#
# ★ 2026-09-21 用户需求：「后台增加客户端设备，也能管理客户端设备，比如管理员
#   直接能从后台启动安卓模拟器（这是其中一种设备），然后还能在后台添加安卓手机
#   实体机客户端」。
#
# 模型（见 db.py devices 表那段注释）：
#   clients = 一台跑 agent.py 的电脑（心跳/在线状态）
#   devices = 那台电脑上挂着的具体设备（模拟器 / USB 实体机），设备级才有账号指派
#
# 后端**永远不直接碰 ADB** —— 它在内网另一头。所有设备操作都是
# 「往客户端下一次指令 → 客户端执行 → 回报结果」，所以每个按钮的反馈都是
# 「已下发，等客户端回报」，结果回到这一页的设备块里。

def _anyone_running() -> Optional[Dict[str, Any]]:
    """本机（所有客户端）此刻有没有在跑任务。

    ★ 用途是用户需求里那句「添加完之后后台会自动查看目前有没有在跑任务，
      如果没有跑会提示可以添加然后启动模拟器或者实体手机」。

    判据看两处，缺一不可：
      · 客户端心跳里报的 busy/state=running（正在跑）
      · 客户端有**未取走**的一轮任务在队列里（即将跑）
    只查队列会漏掉「正在跑但队列已清空」；只查状态会漏掉「刚派下去还没起」。
    """
    for c in db.client_list():
        st = c.get("status") or {}
        if st.get("busy") or st.get("state") == "running":
            return {"client": c, "why": "客户端「%s」正在跑任务"
                    % (c.get("name") or c.get("host") or c.get("uid"))}
    pend = db.request_pending(limit=1)
    if pend:
        return {"client": None,
                "why": "队列里还有 %d 条待执行任务" % len(db.request_pending(limit=99))}
    return None


def _device_post(request: Request, cid: int, kind: str, extra: Optional[Dict[str, Any]] = None,
                 note: str = "", back: str = "") -> RedirectResponse:
    """给某台客户端的设备下发一条指令（设备页的按钮都走这里，避免各写一遍）。

    实时通道连着就是秒到；没连上则等下一次心跳（≤ HEARTBEAT_INTERVAL 秒）取走。
    """
    cli = db.client_get(cid)
    if not cli:
        return RedirectResponse("/devices?err=%s" % _q("客户端不存在"), status_code=303)
    p = db.client_request_probe(cid, kind=kind,
                                by=current_user(request) or "?", note=note,
                                extra=extra or {})
    pushed = realtime.notify_client(cid, "command", probe=p.get("id"))
    db.event("info", "console", "给客户端 #%d 下发设备指令「%s」%s"
             % (cid, db.PROBE_KINDS.get(kind, kind), "（实时推送）" if pushed else ""))
    tip = "已下发「%s」%s" % (db.PROBE_KINDS.get(kind, kind),
                              "（实时通道已送达）" if pushed
                              else "，等它下次心跳（最多 %d 秒）" % settings.HEARTBEAT_INTERVAL)
    return RedirectResponse((back or "/devices?hl=%d" % cid) + "&ok=%s" % _q(tip),
                            status_code=303)


@router.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, hl: int = 0, ok: str = "", err: str = "",
                 cid: int = 0):
    """设备页：扫描 ADB 设备、添加实体机/模拟器、远程启停模拟器、横屏覆盖、发现角色。

    管理员专属 —— 这一页全都是对共用主机的操作（启停模拟器谁都能点的话，
    等于谁都能把别人正在跑的任务打断）。
    """
    require_admin(request)
    sc = scope_of(request)
    clients = db.client_list()
    devs = db.device_list()
    # 设备按「属于哪台电脑」分组渲染；没归属的（电脑记录被删过）单独一堆
    by_client: Dict[int, List[Dict[str, Any]]] = {}
    for d in devs:
        by_client.setdefault(int(d.get("client_id") or 0), []).append(d)
        d["_account"] = db.account_get(int(d["account_id"])) if d.get("account_id") else None
    for c in clients:
        c["_devices"] = by_client.get(int(c["id"]), [])
        c["_roles"] = [r for r in (db.role_list(int(c["account_id"]))
                                   if c.get("account_id") else []) if r.get("enabled")]
        c["realtime"] = realtime.is_connected(c.get("uid") or "")
        # 这台电脑上一次 adb_scan 的结果里，有哪些设备**还没登记**
        pr = c.get("probe") or {}
        c["_scan_new"] = []
        if pr.get("kind") == "adb_scan" and pr.get("status") == "done":
            res = pr.get("result") or {}
            known = {str(d["serial"]) for d in c["_devices"]}
            for d in (res.get("devices") or []):
                s = str(d.get("serial") or "")
                if s and s not in known:
                    c["_scan_new"].append(d)
            c["_emu"] = res.get("emulator") or {}
    return templates.TemplateResponse(request, "devices.html", _ctx(
        request, nav="devices", clients=clients, devices=devs, hl=hl,
        orphan=by_client.get(0, []), accounts=db.account_list(**sc),
        role_list=db.role_list(**sc), running=_anyone_running(),
        ok=ok, err=err))


@router.post("/devices/scan")
async def devices_scan(request: Request):
    """让某台客户端扫一遍它本机的 ADB 设备（含未授权的，那样才能提示去点允许调试）。

    ★ 扫描结果**不会**自动变成设备记录 —— 只有用户点「添加」才入库。
      理由见 routes_agent._sync_scanned_devices 的说明（自动建会塞进一堆脏行）。
    """
    require_admin(request)
    form = await request.form()
    cid = int(str(form.get("client_id") or "0") or 0)
    if not cid:
        return RedirectResponse("/devices?err=%s" % _q("没指定哪台客户端"), status_code=303)
    return _device_post(request, cid, "adb_scan",
                        note="后台要求扫描 ADB 设备",
                        back="/devices?hl=%d" % cid)


@router.post("/devices/add")
async def device_add(request: Request):
    """登记一台设备（模拟器或 USB 实体机）。

    两种入口共用这一个路由：
      · 扫描结果里的「添加」按钮 → 表单带着扫到的 serial / kind / 屏幕信息
      · 手动填写 → 用户自己敲 serial（比如手机没插着、想先建好记录）
    幂等：同一台电脑下同一个 serial 重复提交只会得到同一条记录（device_create）。
    """
    require_admin(request)
    form = await request.form()
    cid = int(str(form.get("client_id") or "0") or 0)
    serial = str(form.get("serial") or "").strip()
    kind = str(form.get("kind") or "phone")
    name = str(form.get("name") or "").strip()
    note = str(form.get("note") or "").strip()
    if not cid:
        return RedirectResponse("/devices?err=%s" % _q("请先选这台设备挂在哪台客户端下"),
                                status_code=303)
    if not serial:
        return RedirectResponse("/devices?hl=%d&err=%s" % (cid, _q("serial 不能为空")),
                                status_code=303)
    did = db.device_create(serial, kind, client_id=cid, name=name, note=note,
                           vmindex=int(str(form.get("vmindex") or "0") or 0))
    # 顺手把扫到的屏幕信息补进去（有就填，没有就算了）
    for k in ("screen_w", "screen_h", "native_w", "native_h"):
        v = str(form.get(k) or "").strip()
        if v.isdigit():
            db.device_update(did, **{k: int(v)})
    if str(form.get("override") or "") == "1080x1920":
        db.device_update(did, force_size=1)
    row = db.device_get(did)
    db.event("info", "console", "登记设备 %s（%s）到客户端 #%d"
             % (serial, db.DEVICE_KINDS.get(kind, kind), cid))
    return RedirectResponse("/devices?hl=%d&ok=%s"
                            % (cid, _q("已添加设备：%s" % ((row or {}).get("name") or serial))),
                            status_code=303)


@router.post("/devices/{did}/update")
async def device_update(request: Request, did: int):
    """改设备的名字 / 类型 / 备注 / 是否启用 / 模拟器实例号。

    只覆盖**表单里真的出现了**的字段（`_has_*` 隐藏标记那一套，和账号编辑一致）：
    设备页上有好几种小表单（有的只改名字），缺了标记就会把没提交的字段清空。
    """
    require_admin(request)
    form = await request.form()
    d = db.device_get(did)
    if not d:
        return RedirectResponse("/devices?err=%s" % _q("设备不存在"), status_code=303)
    fields: Dict[str, Any] = {}
    if "name" in form:
        fields["name"] = str(form.get("name") or "").strip()[:60]
    if "note" in form:
        fields["note"] = str(form.get("note") or "").strip()[:200]
    if form.get("kind") in ("emulator", "phone"):
        fields["kind"] = str(form.get("kind"))
    if "vmindex" in form:
        try:
            fields["vmindex"] = int(str(form.get("vmindex") or "0"))
        except Exception:
            pass
    if form.get("_has_enabled"):
        fields["enabled"] = form.get("enabled") is not None
    db.device_update(did, **fields)
    db.event("info", "console", "设备 #%d 信息已更新（%s）"
             % (did, "、".join(fields.keys()) or "无变化"))
    return RedirectResponse("/devices?hl=%d&ok=%s"
                            % (int(d.get("client_id") or 0), _q("已保存设备信息")),
                            status_code=303)


@router.post("/devices/{did}/assign")
async def device_assign(request: Request, did: int):
    """给设备指派账号 / 角色。角色必须属于该账号（db 层会再校验一次并清空不合法的）。"""
    require_admin(request)
    form = await request.form()
    d = db.device_get(did)
    if not d:
        return RedirectResponse("/devices?err=%s" % _q("设备不存在"), status_code=303)
    raw_a = str(form.get("account_id") or "").strip()
    raw_r = str(form.get("role_id") or "").strip()
    aid = int(raw_a) if raw_a.isdigit() else None
    rid = int(raw_r) if raw_r.isdigit() else None
    if aid is not None and not db.account_get(aid):
        return RedirectResponse("/devices?hl=%d&err=%s"
                                % (int(d.get("client_id") or 0), _q("账号不存在")),
                                status_code=303)
    if aid is not None and rid is not None:
        role = db.role_get(rid)
        if not role or int(role["account_id"]) != aid:
            return RedirectResponse("/devices?hl=%d&err=%s"
                                    % (int(d.get("client_id") or 0),
                                       _q("角色不属于该账号，已忽略角色")),
                                    status_code=303)
    db.device_set_account(did, aid, rid)
    # 设备改了指派 → 对应客户端跑任务时要用新账号，推一份新状态过去
    if d.get("client_id"):
        realtime.push_state_to_client(int(d["client_id"]))
    acc = db.account_get(aid) if aid else None
    label = acc["label"] if acc else "不指派"
    if rid:
        label += " / %s" % (db.role_get(rid) or {}).get("name", "")
    db.event("info", "console", "设备 #%d 指派为 %s" % (did, label))
    return RedirectResponse("/devices?hl=%d&ok=%s"
                            % (int(d.get("client_id") or 0), _q("已指派：%s" % label)),
                            status_code=303)


@router.post("/devices/{did}/delete")
def device_delete(request: Request, did: int):
    require_admin(request)
    d = db.device_get(did)
    cid = int((d or {}).get("client_id") or 0)
    if db.device_delete(did):
        db.event("warn", "console", "删除设备 #%d" % did)
    return RedirectResponse("/devices?hl=%d&ok=%s" % (cid, _q("已删除该设备记录")),
                            status_code=303)


# ---- 远程启停模拟器 ----

@router.post("/devices/emulator/start")
async def emulator_start(request: Request):
    """★ 用户需求的核心动作之一：管理员直接从后台启动安卓模拟器。

    「有没有在跑任务」先查一遍：正在跑就拒绝启动 —— 中途启模拟器会把
    正在跑的任务的界面抢掉（任务那边以为还在主城，实际已经被顶掉了）。
    这不是客套话，是实打实的会把一轮任务跑废。
    """
    require_admin(request)
    form = await request.form()
    cid = int(str(form.get("client_id") or "0") or 0)
    busy = _anyone_running()
    if busy:
        return RedirectResponse("/devices?hl=%d&err=%s"
                                % (cid, _q("现在不能启动模拟器：%s，先等它跑完"
                                           % busy["why"])), status_code=303)
    return _device_post(request, cid, "start_emulator",
                        note="后台启动模拟器", back="/devices?hl=%d" % cid)


@router.post("/devices/emulator/stop")
async def emulator_stop(request: Request):
    require_admin(request)
    form = await request.form()
    cid = int(str(form.get("client_id") or "0") or 0)
    busy = _anyone_running()
    if busy:
        return RedirectResponse("/devices?hl=%d&err=%s"
                                % (cid, _q("现在不能关模拟器：%s" % busy["why"])),
                                status_code=303)
    return _device_post(request, cid, "stop_emulator",
                        note="后台关闭模拟器", back="/devices?hl=%d" % cid)


@router.post("/devices/{did}/force-size")
def device_force_size(request: Request, did: int):
    """把实体机屏幕覆盖成 1920×1080（实体机不覆盖就不能跑：坐标全偏）。

    模拟器不用点这个 —— 它天生就是 1920×1080。
    """
    require_admin(request)
    d = db.device_get(did)
    if not d:
        return RedirectResponse("/devices?err=%s" % _q("设备不存在"), status_code=303)
    cid = int(d.get("client_id") or 0)
    if not cid:
        return RedirectResponse("/devices?err=%s" % _q("这台设备没挂在任何客户端下"),
                                status_code=303)
    return _device_post(request, cid, "force_size", extra={"serial": d["serial"]},
                        note="后台要求把 %s 覆盖成横屏" % d["serial"],
                        back="/devices?hl=%d" % cid)


@router.post("/devices/{did}/restore-size")
def device_restore_size(request: Request, did: int):
    """恢复实体机原生分辨率（拔线前该点一下，否则手机桌面会变形）。"""
    require_admin(request)
    d = db.device_get(did)
    if not d:
        return RedirectResponse("/devices?err=%s" % _q("设备不存在"), status_code=303)
    cid = int(d.get("client_id") or 0)
    return _device_post(request, cid, "restore_size", extra={"serial": d["serial"]},
                        note="后台要求恢复 %s 的原生分辨率" % d["serial"],
                        back="/devices?hl=%d" % cid)


# ---- 角色自动发现 ----

@router.post("/devices/{did}/discover")
async def device_discover(request: Request, did: int):
    """★ 用户需求：「第一次登录游戏后角色自动保存到后端；
    以后登录时游戏里找不到对应角色了，就重新发现并添加到后端」。

    这个按钮让客户端去游戏里读角色名，回报后由
    routes_agent._sync_discovered_roles 写进 game_roles。

    allow_tap：文本里勾了「允许点一次『点击换区』」才传 true ——
    默认只读，这样第一次接入陌生设备时不会做任何点击。
    """
    require_admin(request)
    form = await request.form()
    d = db.device_get(did)
    if not d:
        return RedirectResponse("/devices?err=%s" % _q("设备不存在"), status_code=303)
    cid = int(d.get("client_id") or 0)
    if not cid:
        return RedirectResponse("/devices?err=%s" % _q("这台设备没挂在任何客户端下"),
                                status_code=303)
    busy = _anyone_running()
    if busy:
        return RedirectResponse("/devices?hl=%d&err=%s"
                                % (cid, _q("现在不能发现角色：%s" % busy["why"])),
                                status_code=303)
    extra = {"serial": d["serial"],
             "account_id": d.get("account_id"),
             "allow_tap": "1" if form.get("allow_tap") else "",
             "mark_missing": "1"}
    return _device_post(request, cid, "discover_roles", extra=extra,
                        note="后台要求读取 %s 上的角色" % d["serial"],
                        back="/devices?hl=%d" % cid)


@router.post("/accounts/{aid}/discover")
async def account_discover(request: Request, aid: int):
    """针对**账号**发起角色发现（不指定设备，用这台客户端当前能连上的设备）。

    和 /devices/{did}/discover 的区别：那个指定设备，这个只指定账号 ——
    适用于「账号刚加完、还没把设备和账号绑起来」的时候。
    """
    require_admin(request)
    form = await request.form()
    cid = int(str(form.get("client_id") or "0") or 0)
    acc = db.account_get(aid)
    if not acc:
        return RedirectResponse("/accounts?err=%s" % _q("账号不存在"), status_code=303)
    if not cid:
        return RedirectResponse("/accounts?hl=%d&err=%s"
                                % (aid, _q("请选一台客户端来执行发现")), status_code=303)
    # 优先点名「这台客户端下指派了这个账号的设备」的 serial
    serial = ""
    for d in db.device_list(client_id=cid):
        if d.get("account_id") and int(d["account_id"]) == aid:
            serial = d["serial"]
            break
    extra = {"account_id": aid,
             "allow_tap": "1" if form.get("allow_tap") else "",
             "mark_missing": "1"}
    if serial:
        extra["serial"] = serial
    p = db.client_request_probe(cid, kind="discover_roles",
                                by=current_user(request) or "?",
                                note="后台为账号「%s」发起角色发现" % acc["label"],
                                extra=extra)
    pushed = realtime.notify_client(cid, "command", probe=p.get("id"))
    db.event("info", "console", "为账号「%s」下发角色发现到客户端 #%d%s"
             % (acc["label"], cid, "（实时推送）" if pushed else ""))
    return RedirectResponse("/accounts?hl=%d&ok=%s"
                            % (aid, _q("已下发角色发现%s"
                                       % ("（实时通道已送达）" if pushed
                                          else "，等客户端心跳（最多 %d 秒）"
                                               % settings.HEARTBEAT_INTERVAL))),
                            status_code=303)


@router.get("/api/devices")
def api_devices(request: Request):
    """给设备页自动刷新用：在线状态 + 最近一次指令结果，轻量。"""
    require_admin(request)
    return JSONResponse({
        "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
        "offline_after": settings.CLIENT_OFFLINE_AFTER,
        "clients": [{
            "id": c["id"], "name": c.get("name"), "host": c.get("host"),
            "online": c["online"], "realtime": realtime.is_connected(c.get("uid") or ""),
            "state": (c.get("status") or {}).get("state"),
            "busy": bool((c.get("status") or {}).get("busy")),
            "probe": c.get("probe") or {},
        } for c in db.client_list()],
        "devices": [{
            "id": d["id"], "client_id": d.get("client_id"), "serial": d.get("serial"),
            "kind": d.get("kind"), "name": d.get("name"), "online": bool(d.get("online")),
            "enabled": bool(d.get("enabled")), "force_size": bool(d.get("force_size")),
            "account_id": d.get("account_id"), "role_id": d.get("role_id"),
            "screen_w": d.get("screen_w"), "screen_h": d.get("screen_h"),
        } for d in db.device_list()],
    })


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
    """管理员在设置页改自己的口令。

    与 /password 共用 `_apply_password_change` —— 早先这里单独实现，只查了长度，
    结果是「管理员能把口令改成 12345678，普通用户却不能」，同一条规则两处硬编码。
    """
    u = require_admin(request)
    form = await request.form()
    return _apply_password_change(u, form, "/settings")


# ------------------------------------------------------------------ 事件 & JSON

@router.get("/events", response_class=HTMLResponse)
def events_page(request: Request):
    require_admin(request)
    return templates.TemplateResponse(request, "events.html", _ctx(request, nav="events", events=db.events_recent(200)))


# ================================================================== 用户管理（管理员）
#
# 用户是**后台创建**的，没有自助注册 —— 这套系统跑在一台共用的主机上，
# 谁能用主机是管理员说了算。让任何人自助注册再等审批，等于给管理员凭空加一道
# 「先看看这人是谁」的活；直接由管理员建号更省事，也更符合实际情况。
#
# 「注册」的落点在这里：管理员建号 → 生成初始口令 → 线下发给对方 →
# 对方首次登录被强制改口令（must_change=1）。这样管理员全程不知道对方最终口令。

@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, ok: str = "", err: str = "", hl: int = 0,
               new_pwd: str = ""):
    require_admin(request)
    return templates.TemplateResponse(request, "users.html", _ctx(
        request, nav="users", users=db.user_list(), ok=ok, err=err, hl=hl,
        new_pwd=new_pwd, me=current_account(request),
        min_pwd=8))


@router.post("/users")
async def user_create(request: Request):
    me = require_admin(request)
    form = await request.form()
    username = str(form.get("username") or "").strip()
    role = str(form.get("role") or db.ROLE_USER)
    role = db.ROLE_ADMIN if role == db.ROLE_ADMIN else db.ROLE_USER

    if not username:
        return RedirectResponse("/users?err=%s" % _q("用户名不能为空"), status_code=303)
    if not re.match(r"^[A-Za-z0-9_.@-]{3,40}$", username):
        return RedirectResponse("/users?err=%s" % _q(
            "用户名只能用字母、数字、下划线、点、@、减号，3~40 位"), status_code=303)
    if db.user_by_name(username):
        return RedirectResponse("/users?err=%s" % _q("这个用户名已经有人用了"), status_code=303)

    # 口令：管理员没填就自动生成一个（比让管理员自己想一个更省事，
    # 而且随机生成的一定比人想的长）。生成的一律要求首登改掉。
    raw = str(form.get("password") or "").strip()
    if raw:
        problem = security.password_problem(raw, username)
        if problem:
            return RedirectResponse("/users?err=%s" % _q("口令不合格：%s" % problem),
                                    status_code=303)
        generated = False
    else:
        raw = security.new_token(12)
        generated = True

    uid = db.user_create(username, security.hash_secret(raw), role=role,
                         display_name=str(form.get("display_name") or ""),
                         contact=str(form.get("contact") or ""),
                         note=str(form.get("note") or ""),
                         created_by=me["username"], must_change=True)
    db.event("info", "console", "管理员「%s」新建用户「%s」（#%d，角色=%s）"
             % (me["username"], username, uid, role))
    # 初始口令**只在这次跳转的 URL 里带上一次**，页面显示完就让用户自己存。
    # 不写日志、不入库 —— 库里只有哈希，这也是刻意的（管理员事后也拿不回来）。
    note = "已创建用户「%s」%s" % (username, "，口令已自动生成" if generated else "")
    return RedirectResponse("/users?hl=%d&ok=%s&new_pwd=%s"
                            % (uid, _q(note + "。请把初始口令交给本人（只显示这一次）"),
                               _q("%s|%s" % (username, raw))),
                            status_code=303)


@router.post("/users/{uid}/update")
async def user_update(request: Request, uid: int):
    me = require_admin(request)
    target = db.user_get(uid)
    if not target:
        return RedirectResponse("/users?err=%s" % _q("没有这个用户"), status_code=303)
    form = await request.form()
    fields: Dict[str, Any] = {}
    for k, lim in (("display_name", 40), ("contact", 120), ("note", 400)):
        if k in form:
            fields[k] = str(form.get(k) or "").strip()[:lim]

    new_name = str(form.get("username") or "").strip()
    if new_name and new_name != target["username"]:
        if not re.match(r"^[A-Za-z0-9_.@-]{3,40}$", new_name):
            return RedirectResponse("/users?hl=%d&err=%s" % (uid, _q("用户名格式不合法")),
                                    status_code=303)
        other = db.user_by_name(new_name)
        if other and int(other["id"]) != uid:
            return RedirectResponse("/users?hl=%d&err=%s" % (uid, _q("这个用户名已经有人用了")),
                                    status_code=303)
        fields["username"] = new_name

    new_role = str(form.get("role") or target.get("role"))
    if new_role in (db.ROLE_ADMIN, db.ROLE_USER):
        # ★ 不能改**自己**的角色。降自己一级会当场丢掉管理员权限 ——
        #   current_account() 每次请求现查库，所以这个请求一提交，
        #   你自己的会话立刻就不再是管理员了：想改回来都点不动（要别人来改）。
        #   要卸任就找另一个管理员，或者用命令行 `demote-user`。
        if int(uid) == int(me["id"]) and new_role != target.get("role"):
            return RedirectResponse(
                "/users?hl=%d&err=%s" % (uid, _q(
                    "不能改自己的角色 —— 降级会当场丢掉管理员权限，之后连这一页都进不来。"
                    "要卸任请让另一个管理员来操作")),
                status_code=303)
        # ★ 不能把最后一个启用的管理员降级/停用 —— 否则谁也进不了后台，
        #   只能拿命令行走 CLI 救，属于自锁。
        if target.get("is_admin") and new_role != db.ROLE_ADMIN:
            if db.user_count_admins() <= 1:
                return RedirectResponse(
                    "/users?hl=%d&err=%s" % (uid, _q(
                        "这是唯一的管理员，不能改成普通用户 —— 改了就没人能进后台了")),
                    status_code=303)
        fields["role"] = new_role

    if form.get("_has_enabled"):
        want_enabled = form.get("enabled") is not None
        if not want_enabled and target.get("is_admin") and db.user_count_admins() <= 1:
            return RedirectResponse(
                "/users?hl=%d&err=%s" % (uid, _q("这是唯一的管理员，不能停用")),
                status_code=303)
        if not want_enabled and int(uid) == int(me["id"]):
            return RedirectResponse("/users?hl=%d&err=%s" % (uid, _q("不能停用自己")),
                                    status_code=303)
        fields["enabled"] = 1 if want_enabled else 0

    db.user_update(uid, **fields)
    db.event("info", "console", "管理员「%s」更新用户 #%d（%s）"
             % (me["username"], uid, "、".join(fields.keys()) or "无变化"))
    return RedirectResponse("/users?hl=%d&ok=%s" % (uid, _q("已保存")), status_code=303)


@router.post("/users/{uid}/reset")
async def user_reset_password(request: Request, uid: int):
    """管理员重置某用户口令：生成新的临时口令，并标记「首次登录须改」。

    ★ 这里**不**把 must_change 设成可选的。重置口令的典型场景就是
      「怀疑账号被盗」或「对方忘了」——两种情况下都希望新口令只是一次性的。
    """
    me = require_admin(request)
    target = db.user_get(uid)
    if not target:
        return RedirectResponse("/users?err=%s" % _q("没有这个用户"), status_code=303)
    raw = security.new_token(12)
    security.set_user_password(uid, raw, must_change=True)
    db.event("warn", "console", "管理员「%s」重置了用户「%s」的口令"
             % (me["username"], target["username"]))
    return RedirectResponse("/users?hl=%d&ok=%s&new_pwd=%s"
                            % (uid, _q("已重置口令，请交给本人（只显示这一次）"),
                               _q("%s|%s" % (target["username"], raw))),
                            status_code=303)


@router.post("/users/{uid}/delete")
def user_delete(request: Request, uid: int):
    me = require_admin(request)
    target = db.user_get(uid)
    if not target:
        return RedirectResponse("/users?err=%s" % _q("没有这个用户"), status_code=303)
    if int(uid) == int(me["id"]):
        return RedirectResponse("/users?err=%s" % _q("不能删掉自己"), status_code=303)
    if target.get("is_admin") and db.user_count_admins() <= 1:
        return RedirectResponse("/users?err=%s" % _q("这是唯一的管理员，不能删"), status_code=303)
    db.user_delete(uid)
    db.event("warn", "console", "管理员「%s」删除用户「%s」（其游戏账号已收归管理员，未删除）"
             % (me["username"], target["username"]))
    return RedirectResponse("/users?ok=%s" % _q(
        "已删除该用户。他登记的游戏账号已收归管理员名下（没跟着删，可在「账号角色」页处理）"),
        status_code=303)


@router.get("/api/summary")
def api_summary(request: Request):
    require_user(request)
    sc = scope_of(request)
    stats = db.run_stats(days=7, **sc)
    last = stats.get("last")
    return JSONResponse({
        "total": stats["total"], "ok_runs": stats["ok_runs"], "bad_runs": stats["bad_runs"],
        "last": None if not last else {
            "id": last["id"], "started_at": last["started_at"], "slot": last["slot"],
            "all_ok": bool(last["all_ok"]), "n_ok": last["n_ok"],
            "n_fail": last["n_fail"], "n_skip": last["n_skip"],
        },
        "pending_jobs": len(db.request_list(limit=99, owner_id=sc["owner_id"],
                                            own_only=sc["own_only"])),
        "config_version": db.config_current()["version"],
        # 客户端在线情况，给总览页自动刷新用
        "clients": db.client_online_count(),
        "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
    })


@router.get("/healthz")
def healthz():
    return {"ok": True, "version": settings.APP_VERSION, "runs": db.run_count()}
