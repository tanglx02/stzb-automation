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

CREATE TABLE IF NOT EXISTS clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uid             TEXT NOT NULL,             -- 客户端持久化的稳定标识
    name            TEXT,                      -- 人给起的名字（「公司这台」「家里那台」）
    host            TEXT,                      -- 机器名，仅作展示
    agent_version   TEXT,
    account_id      INTEGER,                   -- 指派：该客户端负责哪个账号
    role_id         INTEGER,                   -- 指派：该账号下的哪个角色
    note            TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,-- 0 = 暂停派发任务
    first_seen      TEXT,
    last_seen       TEXT,                      -- 心跳时间，在线判定全靠它
    last_ip         TEXT,
    last_status_json TEXT,                     -- 最近一次心跳带的运行状态
    probe_json      TEXT,                      -- 人工探测请求/结果
    created_at      TEXT,
    updated_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_uid ON clients(uid);

CREATE TABLE IF NOT EXISTS game_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,                 -- 展示名，如「主号」「小号A」
    login_name  TEXT,                          -- 网易账号（手机号/邮箱），仅用于对账展示
    masked      TEXT,                          -- 登录页读到的脱敏账号，如 159****4508
    tag         TEXT,                          -- 备注/分组
    note        TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS game_roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES game_accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,                 -- 角色名，OCR 读到的那个
    server      TEXT,                          -- 区服，如「X6014」
    season      TEXT,                          -- 赛季/剧本，如「龙兴之」
    tab         TEXT,                          -- 属于哪个页签：已有角色 / 经典服 / 青春服
    note        TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_roles_account ON game_roles(account_id);

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_run_id     TEXT,                    -- 脚本侧唯一标识，用于重复上传幂等
    host              TEXT,
    client_id         INTEGER,                 -- 哪台客户端跑的
    account_id        INTEGER,                 -- 当时用的是哪个账号
    role_id           INTEGER,                 -- 当时用的是哪个角色
    account_label     TEXT,                    -- 快照：账号展示名（账号删了也留痕）
    role_label        TEXT,                    -- 快照：角色名
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
    client_id   INTEGER,                           -- 指定哪台客户端执行；NULL = 任意一台
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
CREATE INDEX IF NOT EXISTS idx_req_client ON run_requests(client_id, status);

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
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


# 老库升级用：列名 -> 建列语句。SQLite 的 ALTER TABLE ADD COLUMN 不能加约束，
# 所以这里都写最朴素的形态；加完列后由应用层保证语义。
_MIGRATIONS = {
    "runs": {
        "client_id": "INTEGER",
        "account_id": "INTEGER",
        "role_id": "INTEGER",
        "account_label": "TEXT",
        "role_label": "TEXT",
    },
    "run_requests": {
        "client_id": "INTEGER",
    },
}


def _columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except Exception:
        return set()


def _migrate(conn: sqlite3.Connection) -> None:
    """把老版本的库补齐到当前结构。幂等，每次启动都跑一遍，检查一遍很快。"""
    for table, cols in _MIGRATIONS.items():
        have = _columns(conn, table)
        if not have:
            continue                      # 表还不存在（新库已由 SCHEMA 建全）
        for col, decl in cols.items():
            if col not in have:
                try:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
                except Exception:
                    pass


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
            "INSERT INTO runs(client_run_id,host,client_id,account_id,role_id,"
            "account_label,role_label,slot,dry_run,started_at,finished_at,"
            "duration_seconds,n_ok,n_fail,n_skip,all_ok,env_json,notes_json,"
            "runner_version,exit_code,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (crid, data.get("host"), data.get("client_id"), data.get("account_id"),
             data.get("role_id"), data.get("account_label"), data.get("role_label"),
             data.get("slot"), 1 if data.get("dry_run") else 0,
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


def run_list(limit: int = 50, offset: int = 0, only_failed: bool = False,
             client_id: Optional[int] = None, account_id: Optional[int] = None
             ) -> List[sqlite3.Row]:
    where, params = [], []
    if only_failed:
        where.append("(r.all_ok=0 OR r.n_skip>0)")
    if client_id is not None:
        where.append("r.client_id=?")
        params.append(client_id)
    if account_id is not None:
        where.append("r.account_id=?")
        params.append(account_id)
    sql = ("SELECT r.*, c.name AS client_name FROM runs r "
           "LEFT JOIN clients c ON c.id=r.client_id ")
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY r.started_at DESC, r.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with tx() as c:
        return c.execute(sql, params).fetchall()


def run_count(only_failed: bool = False, client_id: Optional[int] = None,
              account_id: Optional[int] = None) -> int:
    where, params = [], []
    if only_failed:
        where.append("(all_ok=0 OR n_skip>0)")
    if client_id is not None:
        where.append("client_id=?")
        params.append(client_id)
    if account_id is not None:
        where.append("account_id=?")
        params.append(account_id)
    sql = "SELECT COUNT(*) AS n FROM runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with tx() as c:
        return int(c.execute(sql, params).fetchone()["n"])


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

def request_create(slot: str, only_tasks: str, dry_run: bool, by: str, note: str = "",
                   client_id: Optional[int] = None) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO run_requests(created_at,created_by,client_id,slot,only_tasks,"
            "dry_run,status,note) VALUES(?,?,?,?,?,?,'pending',?)",
            (now(), by, client_id, slot or "auto", only_tasks or "",
             1 if dry_run else 0, note))
        return int(cur.lastrowid)


def request_list(limit: int = 50, status: Optional[str] = None) -> List[sqlite3.Row]:
    with tx() as c:
        if status:
            return c.execute(
                "SELECT r.*, c.name AS client_name, c.host AS client_host FROM run_requests r "
                "LEFT JOIN clients c ON c.id=r.client_id "
                "WHERE r.status=? ORDER BY r.id DESC LIMIT ?", (status, limit)).fetchall()
        return c.execute(
            "SELECT r.*, c.name AS client_name, c.host AS client_host FROM run_requests r "
            "LEFT JOIN clients c ON c.id=r.client_id "
            "ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()


def request_pending(limit: int = 10, client_id: Optional[int] = None) -> List[sqlite3.Row]:
    """待领取请求（管理端视角）。

    client_id=None → 不过滤，返回全部（界面统计用）。
    客户端视角请用 request_pending_for()，那条路径有归属校验。
    """
    with tx() as c:
        if client_id is None:
            return c.execute("SELECT * FROM run_requests WHERE status='pending' "
                             "ORDER BY id ASC LIMIT ?", (limit,)).fetchall()
        return c.execute(
            "SELECT * FROM run_requests WHERE status='pending' "
            "AND (client_id IS NULL OR client_id=?) ORDER BY id ASC LIMIT ?",
            (client_id, limit)).fetchall()


def request_pending_for(limit: int, client_id: Optional[int]) -> List[sqlite3.Row]:
    """客户端视角：只返回它**有权领取**的任务。

    规则：
      · 已登记且启用的客户端 → 公共任务（client_id IS NULL）+ 点名派给它的
      · 未登记 / 无法识别的客户端 → **只给公共任务**

    第二条是关键。之前这里传 client_id=None 就等于「不过滤」，
    结果一台还没登记（或者故意不带 uid）的客户端能把别人被点名的定向任务看光、
    还能抢走 —— 定向任务的本意就是「只有这台能执行」，漏出去等于白派。
    """
    with tx() as c:
        if client_id is None:
            return c.execute(
                "SELECT * FROM run_requests WHERE status='pending' "
                "AND client_id IS NULL ORDER BY id ASC LIMIT ?", (limit,)).fetchall()
        return c.execute(
            "SELECT * FROM run_requests WHERE status='pending' "
            "AND (client_id IS NULL OR client_id=?) ORDER BY id ASC LIMIT ?",
            (client_id, limit)).fetchall()


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


# ================================================================== 客户端
#
# 服务端在公网、客户端在内网 —— 服务端**不可能**主动连客户端。
# 所以「是否在线」只能由客户端主动上报心跳（POST /api/agent/ping），
# 服务端把 last_seen 记下来，界面按「距今多少秒」判在线/离线。
# 这不是偷懒，是拓扑决定的唯一可行做法。

# 心跳间隔 30 秒；超过 3 个间隔没收到就认为掉线。
HEARTBEAT_INTERVAL = 30
OFFLINE_AFTER = 90


def _row_client(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    from . import settings as _s            # 避免循环导入，就地取
    age = _age_seconds(d.get("last_seen"))
    interval = int(getattr(_s, "HEARTBEAT_INTERVAL", HEARTBEAT_INTERVAL) or HEARTBEAT_INTERVAL)
    offline_after = int(getattr(_s, "CLIENT_OFFLINE_AFTER", OFFLINE_AFTER) or OFFLINE_AFTER)
    d["age_seconds"] = age
    d["online"] = age is not None and age <= offline_after
    d["stale"] = age is not None and offline_after < age <= offline_after * 6
    d["heartbeat_interval"] = interval
    d["offline_after"] = offline_after
    d["status"] = _loads(d.get("last_status_json"), {}) or {}
    d["probe"] = _loads(d.get("probe_json"), {}) or {}
    return d


def _loads(raw: Optional[str], default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except Exception:
        return default


def _age_seconds(stamp: Optional[str]) -> Optional[int]:
    if not stamp:
        return None
    try:
        t = dt.datetime.fromisoformat(stamp)
    except Exception:
        return None
    return max(0, int((dt.datetime.now() - t).total_seconds()))


def client_upsert(uid: str, *, host: str = "", agent_version: str = "",
                  ip: str = "", status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """心跳落库：新客户端自动登记，老客户端更新 last_seen 与状态。返回该行。"""
    uid = (uid or "").strip()
    if not uid:
        return {}
    ts = now()
    with tx() as c:
        row = c.execute("SELECT * FROM clients WHERE uid=?", (uid,)).fetchone()
        if row is None:
            cur = c.execute(
                "INSERT INTO clients(uid,name,host,agent_version,enabled,first_seen,"
                "last_seen,last_ip,last_status_json,created_at,updated_at) "
                "VALUES(?,?,?,?,1,?,?,?,?,?,?)",
                (uid, host or uid[:12], host, agent_version, ts, ts, ip,
                 json.dumps(status or {}, ensure_ascii=False), ts, ts))
            cid = int(cur.lastrowid)
        else:
            cid = int(row["id"])
            sets = ["last_seen=?", "updated_at=?", "host=?", "last_ip=?"]
            vals: List[Any] = [ts, ts, host or row["host"], ip]
            if agent_version:
                sets.append("agent_version=?")
                vals.append(agent_version)
            if status is not None:
                sets.append("last_status_json=?")
                vals.append(json.dumps(status, ensure_ascii=False))
            vals.append(cid)
            c.execute("UPDATE clients SET %s WHERE id=?" % ",".join(sets), vals)
        row = c.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    return _row_client(row) or {}


def client_get(cid: int) -> Optional[Dict[str, Any]]:
    with tx() as c:
        row = c.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    return _row_client(row)


def client_by_uid(uid: str) -> Optional[Dict[str, Any]]:
    with tx() as c:
        row = c.execute("SELECT * FROM clients WHERE uid=?", ((uid or "").strip(),)).fetchone()
    return _row_client(row)


def client_list() -> List[Dict[str, Any]]:
    with tx() as c:
        rows = c.execute("SELECT * FROM clients ORDER BY enabled DESC, "
                         "last_seen DESC, id").fetchall()
    return [_row_client(r) for r in rows]      # type: ignore[misc]


def client_update(cid: int, **fields: Any) -> None:
    allowed = ("name", "note", "enabled", "account_id", "role_id")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append("%s=?" % k)
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(now())
    vals.append(cid)
    with tx() as c:
        c.execute("UPDATE clients SET %s WHERE id=?" % ",".join(sets), vals)


def client_delete(cid: int) -> bool:
    with tx() as c:
        cur = c.execute("DELETE FROM clients WHERE id=?", (cid,))
        return cur.rowcount > 0


def client_set_probe(cid: int, payload: Optional[Dict[str, Any]]) -> None:
    with tx() as c:
        c.execute("UPDATE clients SET probe_json=?, updated_at=? WHERE id=?",
                  (json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                   now(), cid))


# ---- 人工探测 / 一次性指令 ----
#
# 服务端连不上内网客户端，所以「探测」不是服务端主动去连，而是
# **在客户端下次心跳时把指令带回去**，由客户端自己去截屏/OCR，再把结果报回来。
# 效果上等同于「点一下按钮，几秒后看到那台机器当前的界面文字」。
#
# 同一台客户端同一时刻只保留一条指令：新指令覆盖旧的。
# 原因很实际 —— 排队十条「截个屏」没有任何意义，只会让界面更难读。

PROBE_KINDS = {
    "probe": "探测当前界面（截屏 + 识别文字）",
    "switch": "强制重新切换账号 / 角色",
    "run": "强制立刻跑一轮任务",
}


def client_request_probe(cid: int, kind: str = "probe", by: str = "",
                         note: str = "", extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    kind = kind if kind in PROBE_KINDS else "probe"
    payload = {
        "id": "p%d-%d" % (cid, int(dt.datetime.now().timestamp())),
        "kind": kind,
        "status": "pending",
        "requested_by": by,
        "requested_at": now(),
        "note": note,
        "extra": extra or {},
        "result": None,
    }
    client_set_probe(cid, payload)
    return payload


def client_probe_take(cid: int) -> Optional[Dict[str, Any]]:
    """客户端心跳时取走待执行的指令：只返回 pending 的，并把状态改成 running。"""
    with tx() as c:
        row = c.execute("SELECT probe_json FROM clients WHERE id=?", (cid,)).fetchone()
        if not row or not row["probe_json"]:
            return None
        payload = _loads(row["probe_json"], {}) or {}
        if payload.get("status") != "pending":
            return None
        payload["status"] = "running"
        payload["taken_at"] = now()
        c.execute("UPDATE clients SET probe_json=? WHERE id=?",
                  (json.dumps(payload, ensure_ascii=False), cid))
    return payload


def client_probe_finish(cid: int, ok: bool, message: str = "",
                        data: Optional[Dict[str, Any]] = None) -> None:
    with tx() as c:
        row = c.execute("SELECT probe_json FROM clients WHERE id=?", (cid,)).fetchone()
        if not row or not row["probe_json"]:
            return
        payload = _loads(row["probe_json"], {}) or {}
        payload["status"] = "done" if ok else "failed"
        payload["message"] = str(message or "")[:600]
        payload["result"] = data or {}
        payload["finished_at"] = now()
        c.execute("UPDATE clients SET probe_json=? WHERE id=?",
                  (json.dumps(payload, ensure_ascii=False), cid))
    event("info" if ok else "warn", "probe",
          "客户端 #%d 探测%s：%s" % (cid, "完成" if ok else "失败", str(message or "")[:120]))


def client_online_count() -> Dict[str, int]:
    with tx() as c:
        rows = c.execute("SELECT last_seen, enabled FROM clients").fetchall()
    online = offline = paused = 0
    for r in rows:
        if not r["enabled"]:
            paused += 1
            continue
        age = _age_seconds(r["last_seen"])
        if age is not None and age <= OFFLINE_AFTER:
            online += 1
        else:
            offline += 1
    return {"total": len(rows), "online": online, "offline": offline, "paused": paused}


# ================================================================== 游戏账号 / 角色

def account_list(with_roles: bool = True) -> List[Dict[str, Any]]:
    with tx() as c:
        rows = c.execute("SELECT * FROM game_accounts "
                         "ORDER BY sort_order, id").fetchall()
        roles = c.execute("SELECT * FROM game_roles ORDER BY sort_order, id").fetchall()
    out = [dict(r) for r in rows]
    if with_roles:
        bucket: Dict[int, List[Dict[str, Any]]] = {}
        for r in roles:
            bucket.setdefault(int(r["account_id"]), []).append(dict(r))
        for a in out:
            a["roles"] = bucket.get(int(a["id"]), [])
    return out


def account_get(aid: int, with_roles: bool = True) -> Optional[Dict[str, Any]]:
    with tx() as c:
        row = c.execute("SELECT * FROM game_accounts WHERE id=?", (aid,)).fetchone()
        if not row:
            return None
        roles = c.execute("SELECT * FROM game_roles WHERE account_id=? "
                          "ORDER BY sort_order, id", (aid,)).fetchall()
    d = dict(row)
    d["roles"] = [dict(r) for r in roles] if with_roles else []
    return d


def account_create(label: str, login_name: str = "", masked: str = "",
                   tag: str = "", note: str = "") -> int:
    ts = now()
    with tx() as c:
        nxt = c.execute("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM game_accounts").fetchone()["n"]
        cur = c.execute(
            "INSERT INTO game_accounts(label,login_name,masked,tag,note,sort_order,"
            "enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?)",
            (label.strip()[:60] or "未命名账号", login_name.strip()[:120],
             masked.strip()[:40], tag.strip()[:40], note.strip()[:400], int(nxt), ts, ts))
        return int(cur.lastrowid)


def account_update(aid: int, **fields: Any) -> None:
    allowed = ("label", "login_name", "masked", "tag", "note", "sort_order", "enabled")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append("%s=?" % k)
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(now())
    vals.append(aid)
    with tx() as c:
        c.execute("UPDATE game_accounts SET %s WHERE id=?" % ",".join(sets), vals)


def account_delete(aid: int) -> None:
    """删账号：连带删角色；指向它的客户端指派要清空，否则会指向不存在的行。"""
    with tx() as c:
        c.execute("DELETE FROM game_roles WHERE account_id=?", (aid,))
        c.execute("DELETE FROM game_accounts WHERE id=?", (aid,))
        c.execute("UPDATE clients SET account_id=NULL, role_id=NULL WHERE account_id=?", (aid,))


def role_list(account_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with tx() as c:
        if account_id is None:
            rows = c.execute("SELECT * FROM game_roles ORDER BY sort_order, id").fetchall()
        else:
            rows = c.execute("SELECT * FROM game_roles WHERE account_id=? "
                             "ORDER BY sort_order, id", (account_id,)).fetchall()
    return [dict(r) for r in rows]


def role_get(rid: int) -> Optional[Dict[str, Any]]:
    with tx() as c:
        row = c.execute("SELECT * FROM game_roles WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def role_create(account_id: int, name: str, server: str = "", season: str = "",
                tab: str = "", note: str = "") -> int:
    ts = now()
    with tx() as c:
        nxt = c.execute("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM game_roles "
                        "WHERE account_id=?", (account_id,)).fetchone()["n"]
        cur = c.execute(
            "INSERT INTO game_roles(account_id,name,server,season,tab,note,sort_order,"
            "enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
            (account_id, name.strip()[:60] or "未命名角色", server.strip()[:40],
             season.strip()[:40], tab.strip()[:20], note.strip()[:400], int(nxt), ts, ts))
        return int(cur.lastrowid)


def role_update(rid: int, **fields: Any) -> None:
    allowed = ("name", "server", "season", "tab", "note", "sort_order", "enabled")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append("%s=?" % k)
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(now())
    vals.append(rid)
    with tx() as c:
        c.execute("UPDATE game_roles SET %s WHERE id=?" % ",".join(sets), vals)


def role_delete(rid: int) -> None:
    with tx() as c:
        c.execute("DELETE FROM game_roles WHERE id=?", (rid,))
        c.execute("UPDATE clients SET role_id=NULL WHERE role_id=?", (rid,))


def assignment(cid: int) -> Dict[str, Any]:
    """取某台客户端当前被指派的账号/角色，展开成客户端能直接用的样子。"""
    cli = client_get(cid)
    if not cli:
        return {}
    acc = account_get(int(cli["account_id"])) if cli.get("account_id") else None
    role = role_get(int(cli["role_id"])) if cli.get("role_id") else None
    return {"client": cli, "account": acc, "role": role}
