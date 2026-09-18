# -*- coding: utf-8 -*-
"""运维命令行。

    python -m app.cli reset-password          随机生成一个新口令并打印
    python -m app.cli set-password 新口令      直接设成指定口令
    python -m app.cli new-token               轮换 agent 令牌并打印
    python -m app.cli stats                   打印基础统计
    python -m app.cli prune --keep 200         按保留轮数清理历史

在容器里跑：
    docker compose exec app python -m app.cli reset-password
"""
from __future__ import annotations

import argparse
import sys

from . import db, security, settings


def cmd_reset_password(args) -> int:
    pwd = args.password or security.new_token(9)
    user = args.user
    security.set_admin_password(user, pwd)
    db.event("warn", "cli", "管理员口令被重置")
    print("=" * 56)
    print("  新的管理端口令（请立刻保存，这个不会被再打印）")
    print("    用户名: %s" % user)
    print("    口令  : %s" % pwd)
    print("=" * 56)
    return 0


def cmd_new_token(args) -> int:
    tok = security.rotate_agent_token()
    db.event("warn", "cli", "agent 令牌被轮换")
    print("=" * 56)
    print("  新的采集端令牌（请立刻填进脚本 config.json）")
    print("    %s" % tok)
    print("=" * 56)
    return 0


def cmd_stats(args) -> int:
    db.init_db()
    st = db.run_stats(days=args.days)
    print("数据目录: %s" % settings.DATA_DIR)
    print("数据库  : %s" % settings.DB_PATH)
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

    p = sub.add_parser("reset-password", help="随机生成新口令并打印")
    p.add_argument("-u", "--user", default="admin")
    p.add_argument("-p", "--password", default="", help="指定口令（不填则随机）")
    p.set_defaults(fn=cmd_reset_password)

    p = sub.add_parser("set-password", help="把口令设成指定值")
    p.add_argument("password")
    p.add_argument("-u", "--user", default="admin")
    p.set_defaults(fn=lambda a: cmd_reset_password(argparse.Namespace(
        user=a.user, password=a.password)))

    p = sub.add_parser("new-token", help="轮换 agent 令牌并打印")
    p.set_defaults(fn=cmd_new_token)

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
