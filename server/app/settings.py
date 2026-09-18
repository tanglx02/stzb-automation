# -*- coding: utf-8 -*-
"""环境配置。所有敏感值都从环境变量来，代码里不留任何默认口令。"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """加载 server/.env（若存在），只补进环境变量里**没有**的键。

    Docker 部署时这些值由 docker-compose 从 .env 注入；裸机（含 Windows）部署时
    没有 compose，就靠这里直接读 .env —— 两条路共用同一个配置文件，写法一致。
    真正的系统环境变量优先级更高（.env 不覆盖已存在的键）。
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            # 去掉可能手滑加上的首尾引号
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _default_data_dir() -> str:
    r"""数据目录默认值：Docker/Linux 用 /data（挂卷），Windows 用 server\data（就地）。"""
    if os.name == "nt":
        return str(Path(__file__).resolve().parent.parent / "data")
    return "/data"


# 数据目录：数据库 + 上传上来的报告与截图都放这里，挂一个卷就能整体备份
DATA_DIR = Path(_env("STZB_DATA_DIR", _default_data_dir()))
DB_PATH = Path(_env("STZB_DB_PATH", str(DATA_DIR / "stzb.db")))
ARTIFACT_DIR = Path(_env("STZB_ARTIFACT_DIR", str(DATA_DIR / "artifacts")))

# 会话签名密钥。没设的话首次启动会随机生成并存进数据库（重启后仍有效）
SECRET_KEY_ENV = _env("STZB_SECRET_KEY")

# 单次上传体积上限（字节）。报告约 3MB，留足余量；超大直接拒绝，避免磁盘被塞满
MAX_UPLOAD_BYTES = int(_env("STZB_MAX_UPLOAD_BYTES", str(24 * 1024 * 1024)))
# 每轮最多接受多少张截图
MAX_SHOTS_PER_RUN = int(_env("STZB_MAX_SHOTS_PER_RUN", "60"))

# 保留最近多少轮（0 = 不清理）。默认 200 轮，够看很久又不至于撑爆磁盘
KEEP_RUNS = int(_env("STZB_KEEP_RUNS", "200"))

# 登录失败限流
LOGIN_MAX_FAILS = int(_env("STZB_LOGIN_MAX_FAILS", "8"))
LOGIN_WINDOW_SECONDS = int(_env("STZB_LOGIN_WINDOW_SECONDS", "900"))

# 首次启动自动生成的初始账号（不设就随机生成并打印到日志一次）
BOOTSTRAP_ADMIN_USER = _env("STZB_ADMIN_USER", "admin")
BOOTSTRAP_ADMIN_PASSWORD = _env("STZB_ADMIN_PASSWORD")
BOOTSTRAP_AGENT_TOKEN = _env("STZB_AGENT_TOKEN")

# 是否允许管理端触发任务（下一步让脚本领走）
ALLOW_RUN_REQUESTS = _env("STZB_ALLOW_RUN_REQUESTS", "1") not in ("0", "false", "False")

APP_NAME = "率土之滨自动化 · 控制台"
APP_VERSION = "1.0.0"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
