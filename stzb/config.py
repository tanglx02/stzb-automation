# -*- coding: utf-8 -*-
"""配置加载。读 config.json，缺失项用默认值补上，永不因配置不全而崩。"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULTS: Dict[str, Any] = {
    "device": {
        "adb": r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
        "serial_candidates": ["127.0.0.1:7555", "127.0.0.1:16384",
                              "127.0.0.1:5555", "emulator-5554"],
        "package": "com.netease.stzb.netease",
    },
    # 这几段是「本机专属」的，也必须有兜底：config.json 万一丢了或写坏了，
    # 脚本还得能自己把模拟器拉起来，而不是报「找不到 MuMuManager」。
    "emulator": {
        "manager": r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe",
        "vmindex": 0,
        "startup_timeout": 300,
        "shutdown_after": "auto",
        "cold_restart": False,
    },
    "cloud": {
        "enabled": False,
        "base_url": "",
        "token": "",
        "timeout": 90,
        "retries": 3,
        "pull_config": True,
        "pull_jobs": True,
        "upload_shots_per_task": 4,
    },
    "tasks": {
        "gongpin": True, "shuishou": True, "shijing": True,
        "texing": True, "recruit": True, "yanwu": True,
    },
    "recruit": {
        "free": True,
        "half_price": False,          # 默认保守：买不到就不花
        "half_price_max_hufu": 100,
        "auto_buy_hufu": False,
        "hufu_buy_max": 100,
    },
    "texing": {"free_only": True, "wait_free_seconds": 600},
    "yanwu": {"daily_sweep": True, "wait_free_seconds": 180},
    "shijing": {
        "free_item": True,
        "buy_materials": ["赤柱山铁", "小叶紫檀"],
        "material_pay_copper_only": True,
        "material_max_copper": 60000,
    },
    "shuishou": {"max_times": 3},
    "gongpin": {"enabled": True},
    "safety": {
        "never_tap": ["批量购买", "退出", "退出游戏", "确定退出"],
        "max_task_seconds": 240,
        "max_total_seconds": 900,
        "tap_delay": 0.7,
    },
    "logging": {
        "save_screens": True,     # 是否保存每一步的截图
        "cleanup_enabled": True,  # 总开关：跑完是否自动清理过期文件
        "keep_days": 14,          # 报告与文本日志保留天数（0 = 不清理）
        "shots_keep_days": -1,    # 截图保留天数；-1 = 跟随 keep_days
        "shots_max_mb": 3000,     # 截图目录体积上限（MB），0 = 不限
        "log_keep_days": -1,      # 文本日志保留天数；-1 = 跟随 keep_days
        "cleanup_diag": True,     # 是否顺带清理 diag/ 里的过期图片
    },
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(dict):
    """配置对象。**故意继承 dict**，这样远端下发的配置能直接深合并进来、
    环境变量也能直接覆盖某一项。

    同时保留原来的「点号路径」写法：`cfg.get("logging.keep_days", 14)`。
    带点号就走路径查找，不带点号就按普通字典键查（`cfg.get("tasks", {})` 照旧可用）。
    """

    def __init__(self, data: Dict[str, Any], path: str = CONFIG_PATH):
        super().__init__(data)
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        if isinstance(dotted, str) and "." in dotted:
            cur: Any = self
            for part in dotted.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return cur
        return dict.get(self, dotted, default)


def load(path: str = CONFIG_PATH) -> Config:
    raw: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:      # 配置坏了也不能让脚本挂掉
            print("!! config.json 解析失败(%s)，改用默认配置" % e)
            raw = {}
    data = _merge(DEFAULTS, raw)
    _auto_resolve_paths(data)
    return Config(data, path)


# ------------------------------------------------------------------ 路径自动探测
# 目的：别人 clone 下来、或把 MuMu 装在非默认位置，不用手改 config.json 也能跑。
# 只做「探测存在 → 覆盖」，探测不到就保持原值（跑起来时会有明确报错，别静默改坏）。

_MUMU_ROOT_CANDIDATES = [
    r"C:\Program Files\Netease\MuMu",
    r"D:\Program Files\Netease\MuMu",
    r"C:\Program Files (x86)\Netease\MuMu",
    r"D:\Program Files (x86)\Netease\MuMu",
    r"D:\MuMu",
    r"C:\MuMu",
]


def _find_first(paths) -> Optional[str]:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _auto_resolve_paths(data: Dict[str, Any]) -> Dict[str, Any]:
    """adb 和 MuMuManager 的路径若不存在，就去常见安装位置找。"""
    dev = data.setdefault("device", {})
    emu = data.setdefault("emulator", {})

    adb = dev.get("adb")
    if not adb or not os.path.exists(adb):
        found = _find_first(os.path.join(r, "nx_main", "adb.exe")
                            for r in _MUMU_ROOT_CANDIDATES)
        if found:
            dev["adb"] = found

    mgr = emu.get("manager")
    if not mgr or not os.path.exists(mgr):
        found = _find_first(os.path.join(r, "nx_main", "MuMuManager.exe")
                            for r in _MUMU_ROOT_CANDIDATES)
        if found:
            emu["manager"] = found
    return data
