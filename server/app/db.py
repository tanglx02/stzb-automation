# -*- coding: utf-8 -*-
"""SQLite 访问层。单文件数据库，零运维，够这台规模用很久。"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from . import plan, settings

# 区服编号的格式（X6014 / s12345 / S6014 …）。
# ⚠ 只用来「剥掉前缀」，**从不比较区服的具体值** —— 合服后编号会变，
#   比具体值等于埋了个定时炸弹。与采集端 stzb/account.py:_SRV_PREFIX_RE 一致。
_SRV_PREFIX_RE = re.compile(r"^[A-Za-z]{1,3}\d{3,5}")

# 「竖线类」字符统一成一个形状。
#
# 为什么必须做：游戏里的角色名大量用「丨」(U+4E28 CJK 汉字) 当中缀
# （实测「云魇丨奈子」「执剑丨青山」「鸡波长」这类昵称很常见），而
# **用户在后台手打时几乎必然打成 ASCII 竖线 `|`** —— 两个码点长得几乎一样，
# 眼睛分不出来。不做归一化的后果（真会踩）：
#   · 后端登记「执剑|青山」，客户端 OCR 读出「执剑丨青山」→ 后端认为是**两个**
#     不同角色 → 自动发现会重复建角色、指派比对永远不等 → **天天白切一遍**，
#     正是「只认角色名」这套设计要防的问题。
# ⚠ 只归一化「确定是竖线形状」的字符。**绝不包含 `I` / `l` / `1`** ——
#   那些可能是角色名里真正的字母（如「Iron」「lulu」），归一化会误伤真名字。
# 与采集端 stzb/account.py:_VERT_CHARS 保持一致（护栏测试钉住两边一致）。
_VERT_CHARS = "|丨｜│┃∣❘ㅣǀ"
_VERT_RE = re.compile("[%s]" % re.escape(_VERT_CHARS))


def normalize_vert(s: str) -> str:
    """把各种「竖线类」字符统一成中文竖线「丨」。

    见 `_VERT_CHARS` 的说明：游戏角色名常用「丨」，而人手打时用的是 `|`，
    两者必须视为同一个字符，否则「按角色名识别」会一直匹配不上。
    """
    return _VERT_RE.sub("丨", str(s or ""))


def role_key(name: Any) -> str:
    """角色名的归一化比较键 —— **全系统「角色是谁」的唯一判据**。

    游戏里换区 / 合服都会改掉区服编号（X6014 → X6021），但角色名不变。
    所以任何「这是不是同一个角色」的判断，都必须走这个函数，绝不看区服。

    归一化四件事：
      1. 去掉所有空白
      2. **竖线类字符统一成「丨」**（游戏里用中文竖线，人手打的是 ASCII `|`，
         两者必须视为同一个字符 —— 见 normalize_vert 的说明）
      3. 转小写（OCR 与手填的大小写差异不算区别）
      4. 剥掉开头的区服编号前缀，让「X6014龙兴之」「X6021龙兴之」「龙兴之」
         收敛到同一个键（注意：只按格式剥，从不读编号的值）
    """
    s = normalize_vert(re.sub(r"\s+", "", str(name or "")))
    m = _SRV_PREFIX_RE.match(s)
    if m and len(s) > m.end():
        s = s[m.end():]
    return s.lower()

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
    name        TEXT NOT NULL,                 -- ★ 角色名：切换角色时**唯一**的识别依据
    server      TEXT,                          -- 区服（仅备注用，不参与识别，合服会变）
    season      TEXT,                          -- 赛季/剧本（仅备注用，不参与识别）
    tab         TEXT,                          -- 属于哪个页签：已有角色 / 经典服 / 青春服
    task_plan   TEXT,                          -- 该角色的「执行任务模式」JSON，见 plan.py
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
# 后端可能连不上采集端（不在同一网段 / NAT / 防火墙），甚至以后搬到公网。
# 所以「是否在线」只能由客户端主动上报心跳（POST /api/agent/ping），
# 后端把 last_seen 记下来，界面按「距今多少秒」判在线/离线。
# 这不是偷懒，是拓扑决定的唯一稳妥做法（内网同机部署也用同一套，行为一致）。

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


def client_probe_requeue(cid: int, note: str = "") -> bool:
    """把「已取走但没能执行」的指令**放回待执行**（状态改回 pending）。

    用途只有一个，但很关键：

        客户端正在跑任务时收到「探测界面 / 强制切换」，它**不能**同时操作
        游戏界面 —— 探测要截屏 OCR、切换要点按钮，跟正在跑的任务会互相打架，
        结果两边都乱。所以客户端会回报一个 `BUSY:` 开头的失败，
        我们在这里把指令放回队列，等它闲下来自然会再取走执行。

    没有这一步的话：管理端点了「探测」，客户端恰好忙 → 指令被消费掉 →
    **永远不执行**，而界面上还显示「已下发」，非常误导人。

    返回 True 表示确实放回去了（原本是 running 状态）。
    """
    with tx() as c:
        row = c.execute("SELECT probe_json FROM clients WHERE id=?", (cid,)).fetchone()
        if not row or not row["probe_json"]:
            return False
        payload = _loads(row["probe_json"], {}) or {}
        if payload.get("status") != "running":
            return False
        payload["status"] = "pending"
        payload["taken_at"] = None
        payload["deferred_at"] = now()
        if note:
            payload["defer_note"] = str(note)[:200]
        c.execute("UPDATE clients SET probe_json=? WHERE id=?",
                  (json.dumps(payload, ensure_ascii=False), cid))
    return True


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
                tab: str = "", note: str = "", task_plan: str = "") -> int:
    ts = now()
    with tx() as c:
        nxt = c.execute("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM game_roles "
                        "WHERE account_id=?", (account_id,)).fetchone()["n"]
        cur = c.execute(
            "INSERT INTO game_roles(account_id,name,server,season,tab,task_plan,note,"
            "sort_order,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
            (account_id, name.strip()[:60] or "未命名角色", server.strip()[:40],
             season.strip()[:40], tab.strip()[:20], (task_plan or "")[:4000],
             note.strip()[:400], int(nxt), ts, ts))
        return int(cur.lastrowid)


def role_update(rid: int, **fields: Any) -> None:
    allowed = ("name", "server", "season", "tab", "task_plan", "note",
               "sort_order", "enabled")
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


def role_find_by_name(name: str) -> Optional[Dict[str, Any]]:
    """按**角色名**反查已登记的角色。

    ★ 为什么按名字而不是区服：区服会随合服 / 转服变化（X6014 今天叫「X6014」，
    合服后可能变成「X6021」），名字才是稳定的。所以全系统里
    「角色」的唯一识别键就是 `name`，区服/赛季只当备注。

    匹配用 `role_key()`（去空白 + 转小写 + 剥区服前缀），所以
    「X6021龙兴之」也能命中登记为「X6014龙兴之」或「龙兴之」的角色。

    有歧义时（两个不同角色归一化后同名）**不猜**，返回 None ——
    宁可这一条记录归到「未登记」，也不要张冠李戴。

    用途：运行记录上传时如果后端没指派过角色，就靠客户端自报的角色名
    把这条记录归到正确的角色上，让「角色每日执行情况」不漏数据。
    """
    want = role_key(name)
    if not want:
        return None
    hits: List[sqlite3.Row] = []
    with tx() as c:
        for r in c.execute("SELECT * FROM game_roles ORDER BY id").fetchall():
            if role_key(r["name"]) == want:
                hits.append(r)
                if len(hits) > 1:
                    break
    if not hits:
        return None
    if len(hits) > 1:
        # 多个角色只差区服前缀 → 无法确定是哪一个，拒绝猜测
        return None
    return dict(hits[0])


def role_daily_overview(days: int = 7) -> Dict[str, Any]:
    """按角色聚合最近几天的执行情况（「角色执行」页的数据源）。

    返回::

        {
          "days": 7,
          "dates": ["2026-09-20", ...],          # 今天在前
          "roles": [ {id, name, account_label, plan, client,
                      days:[{date, runs, ok, fail, skip,
                             slots:[{slot, run_id, all_ok, started_at,
                                     n_ok, n_fail, n_skip, tasks:[...]}]}],
                      today: <其中一个>, last_run_at, registered} ],
          "attention": [role_id, ...]            # 今天有未完成项的（导航角标用）
        }

    归属规则：优先按服务端指派（`runs.role_id`）；没有指派时按客户端自报的
    角色名（`runs.role_label`）匹配登记的角色名 —— 就是上面那条「按名字识别」。
    """
    days = max(1, min(int(days or 7), 30))
    today = dt.date.today()
    dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(days)]
    since = dates[-1] + "T00:00:00"

    roles = role_list()
    accounts = {int(a["id"]): a for a in account_list(with_roles=False)}
    clients = client_list()

    by_name: Dict[str, Dict[str, Any]] = {}
    by_id: Dict[int, Dict[str, Any]] = {}
    _ambiguous: set = set()
    for r in roles:
        by_id[int(r["id"])] = r
        k = role_key(r["name"])
        if not k:
            continue
        if k in by_name and int(by_name[k]["id"]) != int(r["id"]):
            _ambiguous.add(k)          # 两个角色归一化后同名 → 不给名字匹配
        else:
            by_name[k] = r
    for k in _ambiguous:
        by_name.pop(k, None)

    with tx() as c:
        runs = c.execute(
            "SELECT id,started_at,slot,all_ok,n_ok,n_fail,n_skip,role_id,role_label,"
            "account_id,account_label,client_id,duration_seconds,env_json "
            "FROM runs WHERE started_at >= ? ORDER BY started_at ASC, id ASC",
            (since,)).fetchall()
        tasks_by_run: Dict[int, List[Dict[str, Any]]] = {}
        if runs:
            ids = [int(x["id"]) for x in runs]
            qs = ",".join("?" * len(ids))
            for t in c.execute(
                    "SELECT run_id,key,name,status,seconds,reason FROM task_results "
                    "WHERE run_id IN (%s) ORDER BY run_id, seq" % qs, ids).fetchall():
                tasks_by_run.setdefault(int(t["run_id"]), []).append(dict(t))

    # ---- 把每条运行记录归到某个角色 ----
    # key: ("id", role_id) 或 ("name", 角色名) 或 ("anon", "")
    bucket: Dict[Any, Dict[Any, Dict[str, Any]]] = {}
    for r in runs:
        date = str(r["started_at"] or "")[:10]
        slot = str(r["slot"] or "auto")
        rid = int(r["role_id"]) if r["role_id"] else None
        label = (r["role_label"] or "").strip()
        if rid is None and label:
            hit = by_name.get(role_key(label))
            if hit:
                rid = int(hit["id"])
        if rid is not None:
            key: Any = ("id", rid)
        elif label:
            key = ("name", label)
        else:
            key = ("anon", "")
        slot_map = bucket.setdefault(key, {})
        cell_key = (date, slot)
        cur = slot_map.get(cell_key)
        entry = {
            "date": date, "slot": slot, "run_id": int(r["id"]),
            "all_ok": bool(r["all_ok"]), "started_at": r["started_at"],
            "n_ok": int(r["n_ok"] or 0), "n_fail": int(r["n_fail"] or 0),
            "n_skip": int(r["n_skip"] or 0),
            "duration_seconds": float(r["duration_seconds"] or 0),
            "client_id": r["client_id"],
            "tasks": tasks_by_run.get(int(r["id"]), []),
            "runs": 1,
        }
        if cur is None:
            slot_map[cell_key] = entry
        else:
            # 同一档跑了多次（手动重跑）：以最后一次为准，但累计次数
            entry["runs"] = int(cur.get("runs") or 1) + 1
            slot_map[cell_key] = entry

    # ---- 组装成角色视图 ----
    out_roles: List[Dict[str, Any]] = []
    attention: List[int] = []

    def build(key: Any, role: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        slot_map = bucket.get(key, {})
        day_list = []
        for d in dates:
            slots = [v for (dd, _s), v in sorted(slot_map.items()) if dd == d]
            ok = sum(x["n_ok"] for x in slots)
            fail = sum(x["n_fail"] for x in slots)
            skip = sum(x["n_skip"] for x in slots)
            day_list.append({
                "date": d, "slots": slots,
                "runs": sum(int(x.get("runs") or 1) for x in slots),
                "ok": ok, "fail": fail, "skip": skip,
                "empty": not slots,
            })
        last = ""
        for d in day_list:
            for s in d["slots"]:
                if s["started_at"] and s["started_at"] > last:
                    last = s["started_at"]
        acc_id = int(role["account_id"]) if role and role.get("account_id") else None
        acc = accounts.get(acc_id) if acc_id else None
        cli = None
        if role:
            for c in clients:
                if c.get("role_id") and int(c["role_id"]) == int(role["id"]):
                    cli = {"id": c["id"], "name": c.get("name") or c.get("host"),
                           "online": bool(c.get("online")), "enabled": bool(c.get("enabled"))}
                    break
        today_cell = day_list[0] if day_list else None
        return {
            "registered": role is not None,
            "id": (role or {}).get("id"),
            "name": (role or {}).get("name") or (key[1] if key[0] == "name" else "（未登记的角色）"),
            "enabled": bool((role or {}).get("enabled", 1)),
            "tab": (role or {}).get("tab") or "",
            "server": (role or {}).get("server") or "",
            "season": (role or {}).get("season") or "",
            "account_id": acc_id,
            "account_label": (acc or {}).get("label") or "",
            "plan": plan.loads_plan((role or {}).get("task_plan")),
            "plan_text": plan.summary_text((role or {}).get("task_plan")),
            "client": cli,
            "days": day_list,
            "today": today_cell,
            "last_run_at": last,
        }

    for r in roles:
        cell = build(("id", int(r["id"])), r)
        out_roles.append(cell)
        t = cell.get("today") or {}
        # 「需要关注」= 今天跑过、但有失败项（含整轮未完成）
        if t and not t.get("empty") and (
                t.get("fail") or any(not s["all_ok"] for s in t["slots"])):
            attention.append(int(r["id"]))
    # 有数据但没登记过的角色名也列出来，避免「跑了却看不到」
    for key in bucket:
        if key[0] == "id":
            continue
        out_roles.append(build(key, None))

    out_roles.sort(key=lambda x: (
        0 if x.get("registered") else 1,
        0 if (x.get("today") and not x["today"].get("empty")) else 1,
        str(x.get("name") or "")))
    return {"days": days, "dates": dates, "roles": out_roles, "attention": attention}
