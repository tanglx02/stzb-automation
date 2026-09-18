# -*- coding: utf-8 -*-
"""SQLite 访问层。单文件数据库，零运维，够这台规模用很久。"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from . import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_run_id     TEXT,                    -- 脚本侧唯一标识，用于重复上传幂等
    host              TEXT,
    slot              TEXT,
    dry_run           INTEGER NOT NULL DEFAULT 0,
    started_at        TEXT,
    finished_at       TEXT,
    duration_seconds  REAL,
    n_ok              INTEGER NOT NULL DEFAULT 0,
    n_fail            INTEGER NOT NULL DEFAULT 0,
    n_skip            INTEGER NOT NULL DEFAULT 0,
    all_ok            INTEGER NOT NULL DEFAULT 0,
    env_json          TEXT,
    notes_json        TEXT,
    runner_version    TEXT,
    exit_code         INTEGER,
    created_at        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_client ON runs(client_run_id);

CREATE TABLE IF NOT EXISTS task_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    key         TEXT,
    name        TEXT,
    status      TEXT,
    seconds     REAL,
    reason      TEXT,
    notes_json  TEXT,
    shot_count  INTEGER NOT NULL DEFAULT 0,
    seq         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON task_results(run_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                 -- report | shot
    label       TEXT,                          -- 截图归属的任务 key
    filename    TEXT,                          -- 存盘文件名（在 artifacts/<run_id>/ 下）
    original    TEXT,                          -- 客户端原始文件名
    mime        TEXT,
    size        INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_art_run ON artifacts(run_id);

CREATE TABLE IF NOT EXISTS configs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version       INTEGER NOT NULL,
    payload_json  TEXT NOT NULL,
    note          TEXT,
    updated_by    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS run_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT,
    created_by  TEXT,
    slot        TEXT,
    only_tasks  TEXT,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending|taken|done|failed|cancelled
    taken_at    TEXT,
    taken_by    TEXT,
    finished_at TEXT,
    run_id      INTEGER,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_status ON run_requests(status);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT,
    level      TEXT,
    source     TEXT,
    message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings.DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    settings.ensure_dirs()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ kv

def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
    with tx() as c:
        row = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def kv_set(key: str, value: str) -> None:
    with tx() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))


def event(level: str, source: str, message: str) -> None:
    try:
        with tx() as c:
            c.execute("INSERT INTO events(at,level,source,message) VALUES(?,?,?,?)",
                      (now(), level, source, message[:2000]))
    except Exception:
        pass


# ------------------------------------------------------------------ 配置

def config_current() -> Dict[str, Any]:
    with tx() as c:
        row = c.execute("SELECT * FROM configs ORDER BY version DESC LIMIT 1").fetchone()
    if not row:
        return {"version": 0, "payload": {}, "updated_at": None, "note": ""}
    return {"version": row["version"], "payload": json.loads(row["payload_json"]),
            "updated_at": row["updated_at"], "note": row["note"] or ""}


def config_save(payload: Dict[str, Any], note: str = "", by: str = "") -> int:
    cur = config_current()
    ver = int(cur.get("version") or 0) + 1
    with tx() as c:
        c.execute("INSERT INTO configs(version,payload_json,note,updated_by,updated_at) "
                  "VALUES(?,?,?,?,?)",
                  (ver, json.dumps(payload, ensure_ascii=False), note, by, now()))
    return ver


def config_history(limit: int = 30) -> List[sqlite3.Row]:
    with tx() as c:
        return c.execute("SELECT id,version,note,updated_by,updated_at FROM configs "
                         "ORDER BY version DESC LIMIT ?", (limit,)).fetchall()


# ------------------------------------------------------------------ 运行

def run_create(data: Dict[str, Any]) -> int:
    """新建一轮记录。client_run_id 重复时返回已有 id（幂等，重传不会产生两条）。"""
    crid = data.get("client_run_id")
    if crid:
        with tx() as c:
            row = c.execute("SELECT id FROM runs WHERE client_run_id=?", (crid,)).fetchone()
        if row:
            return int(row["id"])
    with tx() as c:
        cur = c.execute(
            "INSERT INTO runs(client_run_id,host,slot,dry_run,started_at,finished_at,"
            "duration_seconds,n_ok,n_fail,n_skip,all_ok,env_json,notes_json,"
            "runner_version,exit_code,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (crid, data.get("host"), data.get("slot"), 1 if data.get("dry_run") else 0,
             data.get("started_at"), data.get("finished_at"),
             data.get("duration_seconds") or 0,
             int(data.get("n_ok") or 0), int(data.get("n_fail") or 0),
             int(data.get("n_skip") or 0), 1 if data.get("all_ok") else 0,
             json.dumps(data.get("env") or {}, ensure_ascii=False),
             json.dumps(data.get("notes") or [], ensure_ascii=False),
             data.get("runner_version"), data.get("exit_code"), now()))
        return int(cur.lastrowid)


def run_replace_tasks(run_id: int, tasks: Iterable[Dict[str, Any]]) -> None:
    with tx() as c:
        c.execute("DELETE FROM task_results WHERE run_id=?", (run_id,))
        for i, t in enumerate(tasks):
            c.execute(
                "INSERT INTO task_results(run_id,key,name,status,seconds,reason,"
                "notes_json,shot_count,seq) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, t.get("key"), t.get("name"), t.get("status"),
                 t.get("seconds") or 0, t.get("reason") or "",
                 json.dumps(t.get("notes") or [], ensure_ascii=False),
                 int(t.get("shot_count") or 0), i))


def run_get(run_id: int) -> Optional[sqlite3.Row]:
    with tx() as c:
        return c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def run_tasks(run_id: int) -> List[sqlite3.Row]:
    with tx() as c:
        return c.execute("SELECT * FROM task_results WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()


def run_list(limit: int = 50, offset: int = 0, only_failed: bool = False) -> List[sqlite3.Row]:
    where = "WHERE all_ok=0 OR n_skip>0" if only_failed else ""
    with tx() as c:
        return c.execute("SELECT * FROM runs %s ORDER BY started_at DESC, id DESC "
                         "LIMIT ? OFFSET ?" % where, (limit, offset)).fetchall()


def run_count() -> int:
    with tx() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])


def run_stats(days: int = 14) -> Dict[str, Any]:
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    with tx() as c:
        row = c.execute(
            "SELECT COUNT(*) AS total, SUM(all_ok) AS ok_runs, "
            "SUM(CASE WHEN all_ok=0 THEN 1 ELSE 0 END) AS bad_runs "
            "FROM runs WHERE started_at >= ?", (since,)).fetchone()
        last = c.execute("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
        by_task = c.execute(
            "SELECT key,name,status,COUNT(*) AS n FROM task_results WHERE run_id IN "
            "(SELECT id FROM runs WHERE started_at >= ?) GROUP BY key,status",
            (since,)).fetchall()
    return {
        "days": days,
        "total": int(row["total"] or 0),
        "ok_runs": int(row["ok_runs"] or 0),
        "bad_runs": int(row["bad_runs"] or 0),
        "last": last,
        "by_task": [dict(r) for r in by_task],
    }


def artifact_add(run_id: int, kind: str, filename: str, *, label: str = "",
                 original: str = "", mime: str = "", size: int = 0,
                 sha256: str = "") -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO artifacts(run_id,kind,label,filename,original,mime,size,"
            "sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, kind, label, filename, original, mime, size, sha256, now()))
        return int(cur.lastrowid)


def artifact_list(run_id: int) -> List[sqlite3.Row]:
    with tx() as c:
        return c.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY kind DESC, id",
                         (run_id,)).fetchall()


def artifact_get(aid: int) -> Optional[sqlite3.Row]:
    with tx() as c:
        return c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()


def run_delete(run_id: int) -> Optional[sqlite3.Row]:
    row = run_get(run_id)
    if not row:
        return None
    with tx() as c:
        c.execute("DELETE FROM runs WHERE id=?", (run_id,))
    return row


def prune_runs(keep: int) -> List[int]:
    """只保留最近 keep 轮，返回被删掉的 run id（调用方负责删磁盘文件）。"""
    if keep <= 0:
        return []
    with tx() as c:
        rows = c.execute("SELECT id FROM runs ORDER BY started_at DESC, id DESC "
                         "LIMIT -1 OFFSET ?", (keep,)).fetchall()
        ids = [int(r["id"]) for r in rows]
        for i in ids:
            c.execute("DELETE FROM runs WHERE id=?", (i,))
    return ids


# ------------------------------------------------------------------ 待执行任务

def request_create(slot: str, only_tasks: str, dry_run: bool, by: str, note: str = "") -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO run_requests(created_at,created_by,slot,only_tasks,dry_run,"
            "status,note) VALUES(?,?,?,?,?,'pending',?)",
            (now(), by, slot or "auto", only_tasks or "", 1 if dry_run else 0, note))
        return int(cur.lastrowid)


def request_list(limit: int = 50, status: Optional[str] = None) -> List[sqlite3.Row]:
    with tx() as c:
        if status:
            return c.execute("SELECT * FROM run_requests WHERE status=? "
                             "ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        return c.execute("SELECT * FROM run_requests ORDER BY id DESC LIMIT ?",
                         (limit,)).fetchall()


def request_pending(limit: int = 10) -> List[sqlite3.Row]:
    with tx() as c:
        return c.execute("SELECT * FROM run_requests WHERE status='pending' "
                         "ORDER BY id ASC LIMIT ?", (limit,)).fetchall()


def request_take(req_id: int, by: str) -> bool:
    with tx() as c:
        cur = c.execute("UPDATE run_requests SET status='taken',taken_at=?,taken_by=? "
                        "WHERE id=? AND status='pending'", (now(), by, req_id))
        return cur.rowcount > 0


def request_finish(req_id: int, status: str, run_id: Optional[int] = None) -> None:
    with tx() as c:
        c.execute("UPDATE run_requests SET status=?,finished_at=?,run_id=? WHERE id=?",
                  (status, now(), run_id, req_id))


def request_cancel(req_id: int) -> bool:
    with tx() as c:
        cur = c.execute("UPDATE run_requests SET status='cancelled',finished_at=? "
                        "WHERE id=? AND status IN ('pending','taken')", (now(), req_id))
        return cur.rowcount > 0


def events_recent(limit: int = 100) -> List[sqlite3.Row]:
    with tx() as c:
        return c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
