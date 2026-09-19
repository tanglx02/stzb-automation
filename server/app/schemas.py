# -*- coding: utf-8 -*-
"""请求体模型。字段全部宽松（默认值兜底），宁可存下来也不要因为校验失败丢数据。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TaskIn(BaseModel):
    key: str = ""
    name: str = ""
    status: str = "skip"
    seconds: float = 0.0
    reason: str = ""
    notes: List[str] = Field(default_factory=list)
    shot_count: int = 0


class AccountInfo(BaseModel):
    """客户端心跳里带的「我当前在哪个账号/角色」。

    客户端不需要知道后端数据库里的 id，只要把 OCR 读到的账号脱敏串和角色名
    报上来，后端就能对账 —— 这样客户端换账号不用改配置。
    """
    account_label: Optional[str] = None     # 客户端本机记的账号别名（可选）
    masked: Optional[str] = None            # 登录页读到的脱敏账号，如 159****4508
    role: Optional[str] = None              # 当前角色名
    server: Optional[str] = None            # 当前区服
    switched_at: Optional[str] = None       # 最近一次切换的时间


class DeviceStatus(BaseModel):
    emulator_running: Optional[bool] = None
    adb_serial: Optional[str] = None
    game_running: Optional[bool] = None
    foreground: Optional[str] = None


class HeartbeatIn(BaseModel):
    """心跳负载。字段全部可选 —— 老客户端不带负载也能正常探活。"""
    uid: Optional[str] = None               # 客户端持久化的稳定标识
    host: Optional[str] = None
    mode: Optional[str] = None              # standalone | managed
    state: Optional[str] = None             # idle | running | switching | error
    busy: Optional[bool] = None
    note: Optional[str] = None
    current: Optional[AccountInfo] = None
    device: Optional[DeviceStatus] = None
    last_run_at: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class RunIn(BaseModel):
    client_run_id: Optional[str] = None
    host: Optional[str] = None
    uid: Optional[str] = None               # 客户端稳定标识，服务端据此归属到 clients
    mode: Optional[str] = None
    account_label: Optional[str] = None
    role_label: Optional[str] = None
    slot: str = ""
    dry_run: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: float = 0.0
    counts: Dict[str, int] = Field(default_factory=dict)
    all_ok: bool = False
    env: Dict[str, Any] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)
    runner_version: Optional[str] = None
    exit_code: Optional[int] = None
    tasks: List[TaskIn] = Field(default_factory=list)


class AckIn(BaseModel):
    status: str = "done"
    run_id: Optional[int] = None
    note: Optional[str] = None


class ProbeResultIn(BaseModel):
    """客户端上报探测结果。"""
    uid: Optional[str] = None
    ok: bool = True
    message: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)


class ConfigIn(BaseModel):
    payload: Dict[str, Any]
    note: str = ""


class RequestIn(BaseModel):
    slot: str = "auto"
    only: str = ""
    dry_run: bool = False
    note: str = ""
    client_id: Optional[int] = None


class LoginIn(BaseModel):
    user: str
    password: str


class PasswordIn(BaseModel):
    user: str = "admin"
    password: str
