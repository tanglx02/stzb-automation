# -*- coding: utf-8 -*-
"""运维命令行。

    python -m app.cli reset-password          随机生成一个新口令并打印
    python -m app.cli set-password 新口令      直接设成指定口令
    python -m app.cli new-token               轮换 agent 令牌并打印
    python -m app.cli stats                   打印基础统计
    python -m app.cli prune --keep 200         按保留轮数清理历史

    python -m app.cli users                   列出所有用户
    python -m app.cli add-user zhangsan        新建普通用户（口令随机，首登须改）
    python -m app.cli add-user ops --admin     新建管理员
    python -m app.cli disable-user zhangsan    停用（立刻生效，不用等会话过期）

在容器里跑：
    docker compose exec app python -m app.cli reset-password
"""
from __future__ import annotations

import argparse
import sys

from . import db, security, settings


def _print_cred(title: str, user: str, pwd: str, extra: str = "") -> None:
    print("=" * 56)
    print("  %s（请立刻保存，这个不会被再打印）" % title)
    print("    用户名: %s" % user)
    print("    口令  : %s" % pwd)
    if extra:
        print("    %s" % extra)
    print("=" * 56)


def cmd_reset_password(args) -> int:
    """重置某个用户的口令。**用户不存在就顺手建出来**（含管理员）。

    这是「把自己锁在门外」时的唯一救援通道：忘了口令、或者误把唯一的管理员
    降级/停用，都靠它。所以这里刻意不做权限判断、也不要求旧口令 ——
    能让它跑起来的前提是你已经能登进服务器。
    """
    pwd = args.password or security.new_token(9)
    user = args.user
    existing = db.user_by_name(user)
    if existing:
        security.set_user_password(int(existing["id"]), pwd, must_change=args.must_change)
        db.event("warn", "cli", "用户「%s」的口令被重置" % user)
        _print_cred("新的口令", user, pwd,
                    "首次登录会要求改口令" if args.must_change else "")
    else:
        security.set_admin_password(user, pwd)
        db.event("warn", "cli", "用户「%s」不存在，已按管理员新建" % user)
        _print_cred("已新建管理员", user, pwd)
    return 0


def cmd_new_token(args) -> int:
    tok = security.rotate_agent_token()
    db.event("warn", "cli", "agent 令牌被轮换")
    print("=" * 56)
    print("  新的采集端令牌（请立刻填进脚本 config.json）")
    print("    %s" % tok)
    print("=" * 56)
    return 0


def cmd_users(args) -> int:
    db.init_db()
    rows = db.user_list()
    if not rows:
        print("还没有任何用户。跑一次 `python -m app.cli reset-password` 建管理员。")
        return 0
    print("%-4s %-16s %-8s %-6s %-8s %-20s %s"
          % ("id", "用户名", "角色", "状态", "账号数", "最近登录", "显示名"))
    print("-" * 84)
    for u in rows:
        print("%-4d %-16s %-8s %-6s %-8d %-20s %s"
              % (u["id"], u["username"], "管理员" if u["is_admin"] else "普通用户",
                 "启用" if u["enabled"] else "停用", u.get("n_accounts") or 0,
                 u.get("last_login_at") or "从未", u.get("display") or ""))
    print()
    print("共 %d 个用户，其中启用的管理员 %d 个"
          % (len(rows), db.user_count_admins()))
    return 0


def cmd_add_user(args) -> int:
    db.init_db()
    name = args.username
    if db.user_by_name(name):
        print("!! 用户「%s」已经存在。要改口令用 reset-password。" % name)
        return 2
    pwd = args.password or security.new_token(12)
    problem = security.password_problem(pwd, name)
    if problem:
        print("!! 口令不合格：%s" % problem)
        return 2
    role = db.ROLE_ADMIN if args.admin else db.ROLE_USER
    uid = db.user_create(name, security.hash_secret(pwd), role=role,
                         display_name=args.display or "",
                         created_by="cli", must_change=not args.no_change)
    db.event("info", "cli", "新建用户「%s」（#%d，%s）"
             % (name, uid, "管理员" if args.admin else "普通用户"))
    _print_cred("已新建%s「%s」" % ("管理员" if args.admin else "普通用户", name),
                name, pwd,
                "可以在后台「用户」页给他分配账号"
                + ("；首次登录会要求改口令" if not args.no_change else ""))
    return 0


def cmd_user_action(args) -> int:
    db.init_db()
    row = db.user_by_name(args.username)
    if not row:
        print("!! 没有这个用户：%s" % args.username)
        return 2
    uid = int(row["id"])
    if args.action == "disable":
        if row.get("is_admin") and db.user_count_admins() <= 1:
            print("!! 这是唯一的管理员，停用后就没人能进后台了。已拒绝。")
            return 1
        db.user_update(uid, enabled=0)
        print("已停用「%s」——他的会话会立刻失效。" % args.username)
    elif args.action == "enable":
        db.user_update(uid, enabled=1)
        print("已启用「%s」。" % args.username)
    elif args.action == "promote":
        db.user_update(uid, role=db.ROLE_ADMIN)
        print("已把「%s」提为管理员。" % args.username)
    elif args.action == "demote":
        if row.get("is_admin") and db.user_count_admins() <= 1:
            print("!! 这是唯一的管理员，降级后就没人能进后台了。已拒绝。")
            return 1
        db.user_update(uid, role=db.ROLE_USER)
        print("已把「%s」降为普通用户。" % args.username)
    db.event("warn", "cli", "用户「%s」执行 %s" % (args.username, args.action))
    return 0


def cmd_stats(args) -> int:
    db.init_db()
    st = db.run_stats(days=args.days)
    print("数据目录: %s" % settings.DATA_DIR)
    print("数据库  : %s" % settings.DB_PATH)
    print("用户    : %d 人（启用管理员 %d 人）"
          % (len(db.user_list()), db.user_count_admins()))
    print("最近 %d 天：共 %d 轮，全部成功 %d 轮，有未完成项 %d 轮"
          % (st["days"], st["total"], st["ok_runs"], st["bad_runs"]))
    last = st["last"]
    if last:
        print("最近一次：#%d %s 档 %s（成功%d/失败%d/跳过%d）"
              % (last["id"], last["slot"], last["started_at"],
                 last["n_ok"], last["n_fail"], last["n_skip"]))
    else:
        print("最近一次：无")
    print("待执行请求：%d 条" % len(db.request_pending(limit=99)))
    print("配置版本：v%d" % db.config_current()["version"])
    return 0


def cmd_prune(args) -> int:
    db.init_db()
    ids = db.prune_runs(args.keep)
    import os
    from .routes_agent import _rm_tree
    for i in ids:
        _rm_tree(os.path.join(str(settings.ARTIFACT_DIR), str(i)))
    print("已清理 %d 轮（保留最近 %d 轮）" % (len(ids), args.keep))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("reset-password", help="重置口令（用户不存在则新建管理员）")
    p.add_argument("-u", "--user", default="admin")
    p.add_argument("-p", "--password", default="", help="指定口令（不填则随机）")
    p.add_argument("--must-change", action="store_true",
                   help="要求他下次登录时改掉（默认直接生效，方便自己救急）")
    p.set_defaults(fn=cmd_reset_password)

    p = sub.add_parser("set-password", help="把口令设成指定值")
    p.add_argument("password")
    p.add_argument("-u", "--user", default="admin")
    p.set_defaults(fn=lambda a: cmd_reset_password(argparse.Namespace(
        user=a.user, password=a.password, must_change=False)))

    p = sub.add_parser("new-token", help="轮换 agent 令牌并打印")
    p.set_defaults(fn=cmd_new_token)

    p = sub.add_parser("users", help="列出所有用户")
    p.set_defaults(fn=cmd_users)

    p = sub.add_parser("add-user", help="新建用户（默认普通用户）")
    p.add_argument("username")
    p.add_argument("-p", "--password", default="", help="指定口令（不填则随机生成）")
    p.add_argument("--admin", action="store_true", help="建成管理员")
    p.add_argument("--display", default="", help="显示名")
    p.add_argument("--no-change", action="store_true", help="不要求首次登录改口令")
    p.set_defaults(fn=cmd_add_user)

    for act, key, helptext in (("disable-user", "disable", "停用用户（会话立刻失效）"),
                               ("enable-user", "enable", "启用用户"),
                               ("promote-user", "promote", "提为管理员"),
                               ("demote-user", "demote", "降为普通用户")):
        p = sub.add_parser(act, help=helptext)
        p.add_argument("username")
        p.set_defaults(fn=cmd_user_action, action=key)

    p = sub.add_parser("stats", help="打印基础统计")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("prune", help="按保留轮数清理历史")
    p.add_argument("--keep", type=int, default=settings.KEEP_RUNS)
    p.set_defaults(fn=cmd_prune)

    args = ap.parse_args(argv)
    settings.ensure_dirs()
    db.init_db()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
