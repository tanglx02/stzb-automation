# -*- coding: utf-8 -*-
"""FastAPI 应用装配。

启动顺序：建目录 → 建表 → 首次引导（生成管理员口令与 agent 令牌并打印一次）
      → 挂会话中间件 → 挂路由 → 挂静态文件
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import db, realtime, security, settings
from .realtime import router as realtime_router
from .routes_agent import router as agent_router
from .routes_ui import router as ui_router

log = logging.getLogger("stzb")


def _banner(creds: dict) -> None:
    if not creds:
        return
    bar = "=" * 62
    print(bar, flush=True)
    print("  首次启动 —— 下面的凭据只显示这一次，请立刻保存", flush=True)
    print(bar, flush=True)
    if creds.get("admin_user"):
        print("  管理端账号 : %s" % creds["admin_user"], flush=True)
        print("  管理端口令 : %s" % creds["admin_password"], flush=True)
    if creds.get("agent_token"):
        print("  采集端令牌 : %s" % creds["agent_token"], flush=True)
        print("  把令牌填进脚本侧 config.json 的 cloud.token", flush=True)
    print(bar, flush=True)
    print("  口令/令牌都做过 scrypt 哈希后才入库，这份明文无法再找回。", flush=True)
    print("  忘了口令可在容器里执行：python -m app.cli reset-password", flush=True)
    print(bar, flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    db.init_db()
    creds = security.bootstrap()
    _banner(creds)
    if not creds:
        log.info("已初始化过，跳过引导。数据目录=%s", settings.DATA_DIR)
    # 实时通道要靠事件循环才能从别的线程推消息（管理端路由是同步函数），
    # 所以这里先把循环记下来；退出时清掉，免得关闭过程中还往里塞回调。
    realtime.bind_loop(asyncio.get_running_loop())
    try:
        yield
    finally:
        realtime.unbind_loop()


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION,
              docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)

# 会话 Cookie：HttpOnly + SameSite=Lax。Secure 交给反代按 HTTPS 决定。
# 控制台只做少量表单交互，4 小时有效。
app.add_middleware(SessionMiddleware,
                   secret_key=security.session_secret(),
                   session_cookie="stzb_session",
                   max_age=4 * 3600,
                   same_site="lax",
                   https_only=False)

_static = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static), name="static")

app.include_router(agent_router)
app.include_router(realtime_router)
app.include_router(ui_router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """给所有响应加上基础安全头。报告页自己那份更严，会覆盖这里。

    ★ script-src 从 'none' 放宽到 'self'（**只放同源外部文件，仍然禁止内联**）——
      这不是放松安全，而是修一个静默失效的 bug：
      原来写死 'none'，而控制台页面里用的是内联脚本和内联 onsubmit= 确认框，
      浏览器一律不执行、且**页面上不报任何错**（只有开发者控制台里才有）。
      后果是「删除客户端 / 删除运行记录 / 轮换采集端令牌」这些不可逆操作
      **没有二次确认框**，角色下拉联动和在线状态自动刷新也全都没生效。
      现在：脚本一律放 /static/*.js，页面里零内联 → 'self' 就够了，
      不需要 'unsafe-inline'，XSS 防护强度不变。
    """
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'self'",
    )
    return resp


@app.exception_handler(StarletteHTTPException)
async def http_exc(request: Request, exc: StarletteHTTPException):
    """未登录时 require_admin 抛 303 + Location，这里转成真正的重定向。

    FastAPI 默认会把 HTTPException 渲染成 JSON，不会理会 Location 头，
    所以必须自己接管一下，否则「没登录 → 跳登录页」会变成一坨 JSON。
    """
    if exc.status_code in (301, 302, 303, 307, 308) and exc.headers:
        loc = exc.headers.get("Location")
        if loc:
            return RedirectResponse(loc, status_code=exc.status_code)
    if request.url.path.startswith("/api/") or request.url.path.startswith("/api/agent"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 404:
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<body style=\"font-family:system-ui;padding:60px;text-align:center;color:#1c1e21\">"
            "<h2 style='font-weight:500'>没有这个页面</h2>"
            "<p style='color:#6b7280'><a href='/'>回控制台</a></p></body>",
            status_code=404)
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<body style=\"font-family:system-ui;padding:60px;text-align:center\">"
        "<h2 style='font-weight:500;color:#b3261e'>%d</h2><p>%s</p>"
        "<p><a href='/'>回控制台</a></p></body>" % (exc.status_code, exc.detail),
        status_code=exc.status_code)

