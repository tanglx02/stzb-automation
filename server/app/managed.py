# -*- coding: utf-8 -*-
"""远端可管配置的白名单。

**这是整套系统里最关键的一条安全边界。**

采集脚本的 config.json 里有几类东西绝对不能由服务端下发：
  · `device`   —— adb 路径、要连的端口。改错了脚本直接连不上模拟器
  · `emulator` —— MuMuManager 路径、虚拟机索引
  · `cloud`    —— 后端地址和 token（服务端把自己的凭据下发给自己，荒谬且危险）
  · `logging`  —— 本地磁盘保留策略
  · `account`  —— 账号/角色切换开关。后端能「指派切到哪个账号」（那是数据，不是配置），
                  但**不能把切换功能本身关掉** —— 否则服务端被误改后，
                  客户端会停在别人的账号上跑任务，而本机毫无察觉。
                  开关的本机控制权不给服务端。

所以服务端只允许下发下面这些「业务开关」，其余键一律过滤掉。
脚本侧 `stzb/remote_config.py` 有一份**内容相同**的白名单做二次过滤 ——
两边都拦一遍，任何一边写错都不会把本地配置搞坏。
"""
from __future__ import annotations

from typing import Any, Dict

# 段名 -> 允许的键（None 表示该段下的键全放行）
MANAGED_SECTIONS: Dict[str, Any] = {
    "tasks": None,          # 每个任务的总开关
    "recruit": None,
    "shijing": None,
    "shuishou": None,
    "texing": None,
    "yanwu": None,
    "gongpin": None,
    "safety": None,
}

# 明确禁止下发（即使上面写成 None 也拦掉）
DENY_KEYS = {"_说明", "_材料名", "_shutdown说明", "_cold说明"}


def filter_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """只保留白名单内的段，并去掉注释键。"""
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    for section, allowed in MANAGED_SECTIONS.items():
        if section not in payload:
            continue
        val = payload[section]
        if not isinstance(val, dict):
            continue
        if allowed is None:
            out[section] = {k: v for k, v in val.items() if k not in DENY_KEYS}
        else:
            out[section] = {k: v for k, v in val.items()
                            if k in allowed and k not in DENY_KEYS}
    return out


# 服务端内置的初始值。第一次打开控制台时配置页不是空的，
# 而且和脚本侧 config.json 的出厂值一致，不会一上来就把行为改掉。
DEFAULT_MANAGED_CONFIG: Dict[str, Any] = {
    "tasks": {
        "gongpin": True,
        "shuishou": True,
        "shijing": True,
        "texing": True,
        "yanwu": True,
        "recruit": True,
    },
    "yanwu": {
        "daily_sweep": True,
        "wait_free_seconds": 180,
    },
    "recruit": {
        "free": True,
        "half_price": True,
        "half_price_max_hufu": 100,
        "auto_buy_hufu": True,
        "hufu_buy_max": 100,
    },
    "texing": {
        "free_only": True,
        "wait_free_seconds": 600,
    },
    "shijing": {
        "free_item": True,
        "buy_materials": ["赤珠山铁", "小叶紫檀"],
        "material_pay_copper_only": True,
        "material_max_copper": 60000,
    },
    "shuishou": {
        "max_times": 3,
    },
    "gongpin": {
        "enabled": True,
    },
    "safety": {
        "never_tap": ["批量购买", "退出", "退出游戏", "确定退出", "续期", "¥", "支付",
                      "购买礼包", "立即购买"],
        "max_task_seconds": 240,
        "max_total_seconds": 900,
        "tap_delay": 0.7,
    },
}

# 任务清单（用于界面展示中文名与适用档位）。与脚本侧 stzb/tasks.py 的 TASKS 对应。
TASK_META = [
    {"key": "gongpin", "name": "贡品礼包 / 月卡礼包", "desc": "每日玉符", "slots": ["00:00", "12:00"]},
    {"key": "shuishou", "name": "内政税收", "desc": "领取金币，每天 3 次", "slots": ["00:00", "12:00"]},
    {"key": "shijing", "name": "内政市井", "desc": "免费物品 / 铜钱买材料", "slots": ["00:00", "12:00"]},
    {"key": "yanwu", "name": "内政演武", "desc": "每日「扫荡奖励」（每天 00:00 刷新）", "slots": ["00:00", "12:00"]},
    {"key": "texing", "name": "内政特性", "desc": "免费获取 1 张（仅 12:00 档）", "slots": ["12:00"]},
    {"key": "recruit", "name": "招募", "desc": "免费 / 半价（00:00、12:00 各刷一次，每天各 2 次）", "slots": ["00:00", "12:00"]},
]

STATUS_LABEL = {
    "ok": "完成",
    "fail": "失败",
    "skip": "跳过",
    "error": "异常",
}

# 配置项的中文说明。没列到的会直接显示原始路径，不影响使用。
FIELD_LABELS = {
    "tasks.gongpin": "贡品礼包 / 月卡礼包",
    "tasks.shuishou": "内政税收",
    "tasks.shijing": "内政市井",
    "tasks.yanwu": "内政演武（扫荡奖励）",
    "tasks.texing": "内政特性",
    "tasks.recruit": "招募",
    "recruit.free": "抽免费招募（每天 2 次：00:00 / 12:00 各刷一次）",
    "recruit.half_price": "抽半价招募（每天 2 次：00:00 / 12:00 各刷一次，100 虎符）",
    "recruit.half_price_max_hufu": "愿意为半价付的虎符上限",
    "recruit.auto_buy_hufu": "虎符不够时用玉符按 1:1 兑换",
    "recruit.hufu_buy_max": "每次最多兑换多少虎符",
    "texing.free_only": "只抽免费的那张",
    "texing.wait_free_seconds": "免费次数还剩不到这么多秒就等一下",
    "shijing.free_item": "领宝物商队的免费物品",
    "shijing.buy_materials": "要用铜钱买的材料（一行一个）",
    "shijing.material_pay_copper_only": "只接受铜钱价，玉符价不买",
    "shijing.material_max_copper": "单笔铜钱上限",
    "shuishou.max_times": "税收征收次数上限",
    "gongpin.enabled": "启用贡品礼包核对",
    "yanwu.daily_sweep": "每天领一次演武「扫荡奖励」",
    "yanwu.wait_free_seconds": "离可领取只剩这么多秒就等一下",
    "safety.never_tap": "安全黑名单（一行一个，出现在要点按钮名里就放弃点击）",
    "safety.max_task_seconds": "单个任务超时上限（秒）",
    "safety.max_total_seconds": "一轮总超时上限（秒）",
    "safety.tap_delay": "每次点击后的等待（秒）",
}

# 绝对不能由服务端下发的段（脚本侧也再拦一遍）。仅用于界面提示。
LOCAL_ONLY_SECTIONS = ["device", "emulator", "cloud", "logging", "account"]

SECTION_LABEL = {
    "tasks": "总开关（哪些任务要跑）",
    "recruit": "招募",
    "shijing": "市井",
    "shuishou": "税收",
    "texing": "特性",
    "yanwu": "演武",
    "gongpin": "贡品礼包",
    "safety": "安全阀",
}


