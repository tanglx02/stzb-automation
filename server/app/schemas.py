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


class RunIn(BaseModel):
    client_run_id: Optional[str] = None
    host: Optional[str] = None
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


class ConfigIn(BaseModel):
    payload: Dict[str, Any]
    note: str = ""


class RequestIn(BaseModel):
    slot: str = "auto"
    only: str = ""
    dry_run: bool = False
    note: str = ""


class LoginIn(BaseModel):
    user: str
    password: str


class PasswordIn(BaseModel):
    user: str = "admin"
    password: str
