# -*- coding: utf-8 -*-
"""老库迁移冒烟测试 —— 专钉「引用了新增列的索引不能在建表脚本里建」这个坑。

背景（真崩过的线上问题）：老库的业务表已存在、但缺 owner_id / client_id 这些
多用户改造时补上去的列。旧的 init_db() 流程是

    executescript(SCHEMA)   # CREATE TABLE IF NOT EXISTS 被跳过，但 CREATE INDEX 照样跑
    _migrate(conn)          # 这里才 ALTER TABLE 补列

于是第一步就在老表上建 owner_id 索引 → `no such column: owner_id` → 服务起不来。
修复是把这类索引统一挪进 _indexes()，在补列之后建。

本测试造三个库各跑一遍 init_db()，全部必须成功：
  A. 模拟最老的库：业务表存在但缺全部新列（client_id/owner_id/account_id/role_id…）
  B. 用户的真实库（server/data/stzb.db 的副本，存在才算）
  C. 全新空库（顺带回归新装路径没被改坏）
再对 A 跑第二遍，验证幂等（每次启动都会跑 init_db）。

    python tests/test_migrate_old.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server")

FAILS = []


def check(label, ok, extra=""):
    print("  %-56s %s %s" % (label, "✓" if ok else "✗", extra))
    if not ok:
        FAILS.append(label)


# 最老版本的建表语句：故意用「多用户改造之前」的列集合。
# 注意这里**不能**直接引用 db.SCHEMA（它是新的），必须手写老结构，
# 否则测不出迁移 —— 这正是本测试的意义。
OLD_DDL = """
CREATE TABLE game_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL, login_name TEXT, masked TEXT, tag TEXT, note TEXT,
    sort_order INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_run_id TEXT UNIQUE, host TEXT, slot TEXT, dry_run INTEGER DEFAULT 0,
    started_at TEXT, finished_at TEXT, duration_seconds REAL,
    n_ok INTEGER DEFAULT 0, n_fail INTEGER DEFAULT 0, n_skip INTEGER DEFAULT 0,
    all_ok INTEGER, env_json TEXT, notes_json TEXT, runner_version TEXT,
    exit_code INTEGER, created_at TEXT
);
CREATE TABLE run_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT, created_by TEXT, slot TEXT, only_tasks TEXT,
    dry_run INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
    taken_at TEXT, taken_by TEXT, finished_at TEXT, run_id INTEGER, note TEXT
);
CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);
"""


def fresh_dir(tag):
    d = tempfile.mkdtemp(prefix="stzb_mig_%s_" % tag)
    return d


def load_app(data_dir):
    """在指定数据目录下重新导入 app.db，返回模块。"""
    os.environ["STZB_DATA_DIR"] = data_dir
    os.environ["STZB_ADMIN_USER"] = "mig_admin"
    os.environ["STZB_ADMIN_PASSWORD"] = "Migpw123456"
    os.environ["STZB_AGENT_TOKEN"] = "mig_token"
    if SERVER not in sys.path:
        sys.path.insert(0, SERVER)
    import importlib
    from app import settings as st
    importlib.reload(st)
    from app import db as _db
    importlib.reload(_db)
    return st, _db


def cols_of(path, table):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}
    finally:
        c.close()


def idx_of(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        c.close()


# ------------------------------------------------ A. 模拟最老库（缺全部新列）
print("\n== A. 最老的库：业务表存在但缺 owner_id/client_id ==")
DA = fresh_dir("a")
os.makedirs(DA, exist_ok=True)
dbfile = os.path.join(DA, "stzb.db")
c = sqlite3.connect(dbfile)
c.executescript(OLD_DDL)
c.commit()
c.close()
check("起点：老库确实没有 owner_id",
      "owner_id" not in cols_of(dbfile, "game_accounts"))

st, db = load_app(DA)
st.ensure_dirs()
ok = True
err = ""
try:
    db.init_db()                      # ← 修复前这一行就抛 no such column
except Exception as e:
    ok = False
    err = "%s: %s" % (type(e).__name__, e)
check("init_db() 在老库上不再崩溃", ok, err)

if ok:
    check("game_accounts 补出 owner_id", "owner_id" in cols_of(dbfile, "game_accounts"))
    check("runs 补出 owner_id", "owner_id" in cols_of(dbfile, "runs"))
    check("runs 补出 client_id", "client_id" in cols_of(dbfile, "runs"))
    check("run_requests 补出 client_id", "client_id" in cols_of(dbfile, "run_requests"))
    check("run_requests 补出 owner_id", "owner_id" in cols_of(dbfile, "run_requests"))
    ix = idx_of(dbfile)
    for name in ("idx_runs_owner", "idx_req_owner", "idx_req_client",
                 "idx_accounts_owner"):
        check("索引 %s 已建出" % name, name in ix)
    check("users 表已建出且含 username 列",
          "username" in cols_of(dbfile, "users"))

    # 幂等：每次启动都会再跑一遍
    ok2, err2 = True, ""
    try:
        db.init_db()
    except Exception as e:
        ok2, err2 = False, "%s: %s" % (type(e).__name__, e)
    check("再跑一遍 init_db() 仍不崩（幂等）", ok2, err2)

    # 迁移后应用层能正常读写（不是只有 DDL 跑通）
    aid = db.account_create("老库账号")
    check("迁移后能建账号", bool(aid), "id=%s" % aid)
    ai = db.account_get(aid) if hasattr(db, "account_get") else None
    rows = [a for a in db.account_list(owner_id=None, own_only=True)
            if int(a["id"]) == int(aid)]
    check("账号默认归属为 NULL（老数据即管理员）",
          bool(rows) and rows[0]["owner_id"] is None,
          "owner_id=%s" % (rows[0]["owner_id"] if rows else None))
    check("能按归属查询账号", len(db.account_list(owner_id=None, own_only=True)) >= 1)

# ------------------------------------------------ B. 用户真实库副本
print("\n== B. 用户真实库（server/data/stzb.db 副本） ==")
real = os.path.join(SERVER, "data", "stzb.db")
if os.path.exists(real):
    DB = fresh_dir("b")
    shutil.copy2(real, os.path.join(DB, "stzb.db"))
    st2, db2 = load_app(DB)
    st2.ensure_dirs()
    okb, errb = True, ""
    try:
        db2.init_db()
    except Exception as e:
        okb, errb = False, "%s: %s" % (type(e).__name__, e)
    check("真实库副本 init_db() 成功", okb, errb)
    if okb:
        p2 = os.path.join(DB, "stzb.db")
        check("真实库补出 owner_id", "owner_id" in cols_of(p2, "runs"))
        check("真实库索引齐全",
              {"idx_runs_owner", "idx_req_owner", "idx_accounts_owner"} <= idx_of(p2))
else:
    print("  (跳过：本机还没有真实库)")

# ------------------------------------------------ C. 全新空库
print("\n== C. 全新空库（新装路径回归） ==")
DC = fresh_dir("c")
st3, db3 = load_app(DC)
st3.ensure_dirs()
okc, errc = True, ""
try:
    db3.init_db()
except Exception as e:
    okc, errc = False, "%s: %s" % (type(e).__name__, e)
check("空库 init_db() 成功", okc, errc)
if okc:
    p3 = os.path.join(DC, "stzb.db")
    check("空库索引齐全",
          {"idx_runs_owner", "idx_req_owner", "idx_req_client",
           "idx_accounts_owner"} <= idx_of(p3))

for d in (DA, DC):
    shutil.rmtree(d, ignore_errors=True)

print("\n" + "=" * 62)
if FAILS:
    print("有 %d 项未通过：" % len(FAILS))
    for f in FAILS:
        print("  ✗", f)
    sys.exit(1)
print("全部通过")