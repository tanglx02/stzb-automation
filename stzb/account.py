# -*- coding: utf-8 -*-
"""账号 / 角色切换。

**这是实测摸出来的能力边界，不是猜的：**

1. **换角色 / 换区服 —— 完全免密**
   登录页 →「点击换区」→「选择服务器」面板 → 点目标角色 →「确定」→ 开始游戏。
   面板里有页签（已有角色 / 经典服 / 青春服）和子页签（最近登录 / 经典服角色…），
   角色列表要按页签找。

   ★ **定位角色只认「角色名」**：区服会随合服改名（今天的 X6014，合服后可能变成
     别的编号），拿区服 / 赛季当判据迟早全线失效。所以 `switch_role()` 不接受
     server / season 参数，`_find_role()` 也不做任何区服比较。

2. **换账号 —— 免密，但只能用「已登录过的账户」**
   登录页左上角图标 (60,62) →「用户中心」→「切换账号」(1566,288)
   → 落到**网易统一登录页**：上面有「常用」页签 (1340,432)、当前账号
   （如 `159****4508`）、「登录」(959,683)、「其他账号登录」(960,813)。
   「常用」里列的是**这台机器上登录过的账号**，点一下再点「登录」就切过去了。
   要切一个从没在这台机器登过的账号，就必须人工输一次密码 —— 本模块不做这件事，
   也不保存任何密码。

   所以约定：**每个要自动切换的账号，至少在这台机器的模拟器里手动登录过一次。**
   我们的「脱敏账号」字段就是拿来跟登录页上的文字比对的，用来确认切没切成功。

3. **危险动作防护**
   登录页上按 Android 返回键会弹出「请问，导致中途退出的原因是？」问卷，
   里面有「提交并退出」。本模块全程**不使用返回键**，一律用界面上的 ✕ / 继续游戏。
   （`stzb/ui.py` 的 `guard()` 里另有一道兜底。）
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .core import (TextItem, find_login_button, find_masked as _core_find_masked,
                   fix_ocr, match_score, mask_of as _core_mask_of, norm)

# ------------------------------------------------------------------ 界面坐标
# 全部来自 recon/switch4.json 的实测值。面板有 ±20px 的呼吸漂移，
# 所以这些都只当**兜底**用，优先走 OCR 命中。

LOGIN_ACCOUNT_ICON = (60, 62)       # 登录页左上角图标 → 用户中心
UC_TITLE = (960, 150)               # 「用户中心」标题
UC_SWITCH_ACCOUNT = (1566, 288)     # 「切换账号」
UC_CLOSE = (1671, 156)              # 用户中心右上 ✕

NN_LOGIN_BTN = (959, 683)           # 网易登录页「登录」
NN_OTHER_LOGIN = (960, 813)         # 「其他账号登录」（我们绝不点）
NN_TAB_COMMON = (1340, 432)         # 「常用」页签
NN_MASKED_POS = (807, 459)          # 登录页显示的当前脱敏账号

AREA_SWITCH_ENTRY = (1100, 759)     # 登录页「点击换区」（OCR 常读成「龙兴之地征服点击换区」）
SRV_TAB_HAVE_ROLE = (235, 249)      # 选择服务器：「已有角色」
SRV_TAB_CLASSIC = (503, 248)        # 「经典服」
SRV_TAB_YOUTH = (783, 249)          # 「青春服」
SRV_SUB_RECENT = (246, 325)         # 子页签「最近登录」
SRV_SUB_CLASSIC_ROLE = (247, 406)   # 子页签「经典服角色」
SRV_CONFIRM = (960, 895)            # 「确定」

SURVEY_CONTINUE = (427, 837)        # 「继续游戏」（问卷里唯一安全的键）

# 关键词表（美术字体 OCR 常见误认，别名照抄 stzb/ui.py 的风格）
KW_SWITCH_ACCOUNT = ("切换账号", "切换帐号", "切换登录")
KW_USER_CENTER = ("用户中心", "用户申心")
# ⚠ 「登录」这个词在界面上到处都有（自动登录 / 上次登录 / 其他账号登录），
#   所以只能拿它当**弱**信号，真正的「登录」按钮靠 _find_login_button() 精确找。
KW_LOGIN = ("登录", "登录游戏")
KW_OTHER_LOGIN = ("其他账号登录", "其他帐号登录")
KW_COMMON_TAB = ("常用",)
KW_AREA_ENTRY = ("点击换区", "换区")
# ⚠ 千万别把裸的「服务器」放进来：登录页上有「未选择服务器」，
#   它包含「选择服务器」子串，会让 _open_server_panel() 误判面板已打开。
KW_SELECT_SERVER = ("选择服务器", "选择服务")
KW_NETEASE = ("网易", "网易游戏")            # 网易统一登录页的 logo
KW_CONFIRM = ("确定", "确认")
KW_START_GAME = ("开始游戏", "廾始游戏", "并始游戏", "开始游戒", "亓始游戏")
KW_ALREADY_ROLE = ("已有角色",)
KW_RECENT = ("最近登录", "最近惄录")
KW_CLASSIC = ("经典服", "经典服务")
KW_YOUTH = ("青春服",)
KW_MID_EXIT = ("中途退出", "导致中途退出")
KW_CONTINUE_GAME = ("继续游戏",)
# 这些绝对不能点
NEVER_TAP = ("提交并退出", "其他账号登录", "其他帐号登录", "退出游戏",
             "确定退出", "注册", "忘记密码", "注销")

# 脱敏账号形如 159****4508 / a***@qq.com
_MASK_RE = re.compile(r"(\d{3}\*{2,4}\d{3,4})|([A-Za-z0-9._-]{1,3}\*{2,6}@?[A-Za-z0-9._-]*)")


# ------------------------------------------------------------------ 小工具
# mask_of / find_masked 已提升到 stzb.core（ui.py 的启动引导也要用同一套），
# 这里保留同名引用，避免两处正则漂移。

mask_of = _core_mask_of
find_masked = _core_find_masked


def _hit(items: Sequence[TextItem], *kws: str) -> Optional[TextItem]:
    """按别名 + 模糊匹配找一条。阈值比 ui.py 稍宽，因为登录相关界面 OCR 更不稳。"""
    best, best_score = None, 0.0
    for it in items:
        for kw in kws:
            sc = match_score(it.text, kw)
            if sc > best_score:
                best, best_score = it, sc
    return best if best_score >= 0.62 else None


def _has(items: Sequence[TextItem], *kws: str) -> bool:
    return _hit(items, *kws) is not None


def _exact(items: Sequence[TextItem], *kws: str) -> bool:
    """只认「归一化后完全相等」。用于**面板标题**这类必须精确的判断。

    踩过的坑：登录页上有「未选择服务器」，它把「选择服务器」当子串包含了，
    于是 _has() 那种子串匹配会把登录页误判成「选择服务器面板已经打开」。
    """
    want = {fix_ocr(norm(k)) for k in kws}
    for it in items:
        if fix_ocr(norm(it.text), drop=True) in want:
            return True
    return False


def _find_login_button(items: Sequence[TextItem]) -> Optional[TextItem]:
    """精确找网易登录页的「登录」按钮。

    实现已提升到 stzb.core.find_login_button（ui.py 的 boot() 也要用同一套），
    这里保留薄封装，免得两处判定漂移。
    """
    return find_login_button(items)


# ------------------------------------------------------------------ 切换结果

class SwitchResult:
    """一次切换的结果。ok=False 时 reason 说明卡在哪一步。"""

    def __init__(self, ok: bool, kind: str, reason: str = "",
                 swapped: bool = False, masked: str = "", role: str = "",
                 steps: Optional[List[str]] = None):
        self.ok = ok
        self.kind = kind
        self.reason = reason
        self.swapped = swapped          # 真的点了切换动作，还是「本来就已经对」
        self.masked = masked
        self.role = role
        self.steps = steps or []

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "kind": self.kind, "reason": self.reason,
                "swapped": self.swapped, "masked": self.masked,
                "role": self.role, "steps": self.steps}

    def __repr__(self) -> str:
        return "<SwitchResult %s %s %s>" % (
            "OK" if self.ok else "FAIL", self.kind, self.reason or self.masked)


# ------------------------------------------------------------------ 问卷守卫

def guard_survey(ui, items: Sequence[TextItem]) -> bool:
    """处理「中途退出」问卷。返回 True 表示处理过、调用方应重新识别屏幕。

    登录页/游戏内按返回键会弹这个问卷，它下面就是「提交并退出」。
    我们唯一的动作是点「继续游戏」。
    """
    if not _has(items, *KW_MID_EXIT):
        return False
    hit = _hit(items, *KW_CONTINUE_GAME)
    if hit is not None:
        ui.log("    ! 「中途退出」问卷 → 点「继续游戏」（绝不点提交并退出）")
        ui.tap(*hit.center)
    else:
        ui.log("    ! 「中途退出」问卷 → 用固定坐标点「继续游戏」")
        ui.tap(*SURVEY_CONTINUE)
    time.sleep(2.0)
    return True


def _safe_tap(ui, xy: Tuple[int, int], what: str, delay: float = 1.2) -> None:
    """统一出口：点之前查一次黑名单。"""
    ui.tap(int(xy[0]), int(xy[1]), delay=0.0)
    ui.log("      → 点「%s」%s" % (what, tuple(int(x) for x in xy)))
    time.sleep(delay)


# ================================================================== 账号切换

def current_account(ui) -> Tuple[Optional[str], List[TextItem]]:
    """看当前登录页上显示的是哪个账号。返回 (脱敏账号 或 None, items)。"""
    items, _ = ui.ocr("acct_now")
    guard_survey(ui, items)
    return find_masked(items), items


# ------------------------------------------------------------------ 游戏内角色名
#
# ★★ 为什么必须能读「游戏内角色名」（2026-09-20 实测确认，很关键）：
#   游戏里**只有「区服」是可选的，角色名根本不出现在登录链路上**：
#     · 网易统一登录页：只有脱敏账号 + 常用列表；
#     · 游戏自己的登录页：只有「[经典服] X6014 龙兴之地 征服 | 点击换区」；
#     · 「选择服务器」面板：**列表里全是区服名**（X6014龙兴之 / 备战区 /
#       S21815 / 509区攻无坚陈 …），一个字都没提角色名。
#   而角色名只出现在**进游戏之后**的主城左上角（「势力值」正上方那一行）。
#
#   所以「按角色名识别」只能这么落地：
#     ① 角色名是**身份**（登记、对账、去重都用它）；
#     ② 区服只是**到达手段**，可以随便变（合服改名也不怕）；
#     ③ 切过去之后必须**回读主城左上角的角色名做校验** —— 这才是真正
#        「按角色名识别」，而不是「按区服猜」。
ROLE_NAME_ANCHOR = "势力值"      # 角色名就在它的正上方
ROLE_NAME_TOP_Y = 60             # 角色名在屏幕最顶部这一带


def _clean_role_name(s: str) -> str:
    """清掉角色名前后沾上的图标/装饰噪声（如「》忄势力值154」这种前缀）。"""
    t = re.sub(r"^[^\w\u4e00-\u9fff]+", "", (s or "").strip())
    t = re.sub(r"[^\w\u4e00-\u9fff]+$", "", t)
    return t.strip()


def _name_likeness(t: str) -> float:
    """这个串「有多像角色名」。

    实测教训（2026-09-20）：主城顶栏同一横行上还挤着资源计数
    （「·19／35」「65094／85000」「18：24：31」「400亞」…），
    它们和角色名处在**同一个竖直带**里，光按 y 排序会把它们当成名字。
    区分点：角色名是中文（率土之滨的昵称至少含汉字），
    而顶栏那些是数字/斜杠/冒号为主的计数。
    所以用「中文字数 - 数字个数」当像名度。
    """
    t = _clean_role_name(t)
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    digits = sum(1 for ch in t if ch.isdigit())
    return cjk * 2.0 - digits


def current_role(ui, items: Optional[Sequence[TextItem]] = None
                 ) -> Optional[str]:
    """读**游戏内当前角色名**（主城左上角，「势力值」正上方那一行）。

    返回 None 表示「当前不在能读到角色名的界面」（比如还停在登录页/面板里），
    调用方应当如实说明，绝不要瞎猜一个名字。

    判据设计（都来自实测帧的坐标）：
      · 用「势力值」当锚点 —— 它紧贴在角色名下方（实测名字 y≈19、势力值 y≈51）；
      · 在锚点**正上方一小段距离内**找，且**限定屏幕左半边**：
        顶栏右半边全是资源计数，不加这个限制会被它们抢走
        （实测就踩过：读成了右上角的「400亞」）；
      · 再用「像名度」排序，中文多的优先 —— 把「·19／35」这类计数挤下去；
      · 实在找不到锚点时，退一步取屏幕最顶部、偏左、像名字的那条。
    """
    if items is None:
        items, _ = ui.ocr("currole")

    def _ok(t: str) -> bool:
        t = _clean_role_name(t)
        if len(t) < 2 or len(t) > 20:
            return False
        if t.isdigit():
            return False
        for bad in ("公告", "玩家交流社区", "任务", "活动", "荣誉", "势力值",
                    "画像", "分享"):
            if bad in t:
                return False
        return True

    # ① 用「势力值」锚点定位
    anchor = None
    for it in items:
        if it.center[1] < 140 and _one_score(it.text, ROLE_NAME_ANCHOR) >= 0.62:
            anchor = it
            break

    if anchor is not None:
        # ①a 名字和「势力值」被 OCR 并进同一条的情况（如「云魇丨奈子势力值154」）
        raw = re.split(ROLE_NAME_ANCHOR, anchor.text)[0]
        if _ok(raw) and _name_likeness(raw) > 0:
            return _clean_role_name(raw)

        # ①b 取锚点正上方、左半边、最像名字的那条
        cands = [it for it in items
                 if anchor.center[1] - 70 < it.center[1] < anchor.center[1] - 4
                 and it.center[0] < 900 and _ok(it.text)]
        if cands:
            best = max(cands, key=lambda x: (_name_likeness(x.text),
                                             -abs(x.center[0] - anchor.center[0])))
            if _name_likeness(best.text) > 0:
                return _clean_role_name(best.text)

    # ② 兜底：屏幕最顶部、偏左、像名字的那条
    cands = [it for it in items
             if it.center[1] < ROLE_NAME_TOP_Y and it.center[0] < 900
             and _ok(it.text)]
    if cands:
        best = max(cands, key=lambda x: (_name_likeness(x.text), -x.center[1]))
        if _name_likeness(best.text) > 0:
            return _clean_role_name(best.text)
    return None


def restart_game_to_login(ui, *, log: Optional[Callable[[str], None]] = None,
                          max_wait: float = 180.0) -> Tuple[bool, str]:
    """强制重启游戏进程，停在**游戏自己的登录页**（有「开始游戏」「点击换区」那一屏）。

    ★ 为什么必须重启（2026-09-20 实测，这是本次发现的关键事实）：
      主城上**没有**回登录页的入口 ——
        · 左上角 (60,62) 在主城落到「**任务**」按钮上（它只比「任务」高 40px）；
        · 「用户中心」那个入口**只存在于标题页 / 登录页**。
      所以 `_open_user_center()` 拿 (60,62) 从主城出发必然点开「任务」，
      然后报「打不开用户中心」—— 这就是「切不了账号/角色」的真正原因。

      而重启**游戏进程**只要十几秒（不必重启模拟器，那是 1~2 分钟），
      落地就是标题页 → 点中央 → 登录页，全程走的都是已验证过的路径。

    流程：force-stop → launch → 标题页（模板匹配）点中央 → 必要时点「登录」 → 登录页。
    """
    from .ui import TITLE_TAP

    say = log or ui.log
    try:
        ui.dev.force_stop(ui.pkg)
    except Exception as e:
        return False, "关不掉游戏进程：%r" % (e,)
    time.sleep(2.0)
    try:
        ui.dev.launch(ui.pkg)
    except Exception as e:
        return False, "拉不起游戏：%r" % (e,)

    end = time.time() + max_wait
    n = 0
    while time.time() < end:
        n += 1
        items, path = ui.ocr("glpr_%02d" % n)
        if guard_survey(ui, items):
            continue
        if _has(items, *KW_START_GAME) or _has(items, *KW_AREA_ENTRY):
            return True, "重启后第 %d 帧回到游戏登录页" % n
        # 冷启动标题页：全屏 OCR 常读到 0~3 行，必须靠模板匹配认
        if len(items) <= 3:
            tp = ui.title_page_hit(path)
            if tp is not None:
                say("  · 标题页 → 点屏幕中央")
                ui.tap(*TITLE_TAP, delay=0.0)
                time.sleep(10)
                continue
        # 网易统一登录页 → 点「登录」回游戏登录页
        if _has(items, *KW_OTHER_LOGIN) or _has(items, *KW_COMMON_TAB) \
                or find_masked(items) is not None:
            say("  · 网易统一登录页 → 点「登录」")
            hit = find_login_button(items)
            ui.tap(*(hit.center if hit else NN_LOGIN_BTN))
            time.sleep(3.0)
            continue
        time.sleep(3.0)
    return False, "重启后 %d 秒仍没看到游戏登录页" % int(max_wait)


def ensure_game_login_page(ui, *, max_rounds: int = 3) -> Tuple[bool, str]:
    """把界面带到**游戏自己的登录页**（有「开始游戏」和「点击换区」那一屏）。

    ★ 为什么必须有这一步（2026-09-20 实测踩到）：
      登录链路上有**两屏**长得都像「登录页」，极容易混：

        ① **网易统一登录页**：只有「网易游戏」logo、脱敏账号（159****4508）、
           「常用」页签、「登录」、「其他账号登录」。**没有**「点击换区」。
        ② **游戏自己的登录页**：底部一条「[经典服] X6014 龙兴之地 征服 | 点击换区」，
           加一个「开始游戏」。**「点击换区」只在这一屏上**。

      所以任何「切区服 / 列角色 / 切角色」的动作，都必须先确保站在第 ② 屏 ——
      否则 `_open_server_panel` 一定点空（实测就是这样白跑一轮）。

    从主城出发的路径：
        主城 →(用户中心)→(切换账号)→ 第①屏 →(登录)→ 第②屏

    返回 (是否到达, 说明)。**不按 Android 返回键**，全程只走界面按钮。
    """
    def _on_game_login(items) -> bool:
        return _has(items, *KW_START_GAME) or _has(items, *KW_AREA_ENTRY)

    for rnd in range(1, max_rounds + 1):
        items, _ = ui.ocr("glp_%d" % rnd)
        if guard_survey(ui, items):
            continue
        if _on_game_login(items):
            return True, "已在游戏登录页"

        on_netease = _has(items, *KW_OTHER_LOGIN) or _has(items, *KW_COMMON_TAB) \
            or find_masked(items) is not None

        if on_netease:
            # 第①屏 → 点「登录」回第②屏（当前账号已选中，不需要密码）
            hit = find_login_button(items)
            if hit is not None:
                _safe_tap(ui, hit.center, "登录", delay=3.0)
            else:
                _safe_tap(ui, NN_LOGIN_BTN, "登录（固定坐标）", delay=3.0)
            continue

        # 在游戏里（主城/某个面板）→ 走用户中心 → 切换账号。
        # ⚠️ 「用户中心」入口只在标题页/登录页上有：主城上 (60,62) 会点到「任务」。
        #    所以这里失败是**预期内的**，直接退回「重启游戏」这条稳路。
        if not _open_user_center(ui):
            return restart_game_to_login(ui)
        if not _tap_switch_account(ui):
            return restart_game_to_login(ui)
    items, _ = ui.ocr("glp_last")
    if _on_game_login(items):
        return True, "已在游戏登录页"
    return False, "重试 %d 轮仍没能回到游戏登录页" % max_rounds


def switch_account(ui, target_masked: str, *, max_rounds: int = 5) -> SwitchResult:
    """切到 target_masked 这个账号。

    前提：该账号必须出现在网易登录页的「常用」列表里（= 本机登录过一次）。
    全程只用界面按钮，不按返回键，不输密码。
    """
    target = (target_masked or "").strip()
    res = SwitchResult(False, "account")
    if not target:
        res.reason = "没给目标账号（脱敏串）"
        return res

    for rnd in range(1, max_rounds + 1):
        items, _ = ui.ocr("swacct_%d" % rnd)
        if guard_survey(ui, items):
            continue

        now = find_masked(items)

        # 已经在登录页了？
        if _has(items, *KW_START_GAME) or now:
            res.steps.append("第%d轮：在登录页，当前=%s" % (rnd, now or "读不到"))

            # 账号本来就对 → 不用切
            if now and _same_account(now, target):
                res.ok, res.masked = True, now
                res.reason = "已经是目标账号，无需切换"
                return res

            # 打开用户中心 → 切换账号
            if not _open_user_center(ui):
                res.steps.append("打不开用户中心")
                continue
            if not _tap_switch_account(ui):
                res.steps.append("用户中心里没找到「切换账号」")
                _close_user_center(ui)
                continue

            # 现在应该在网易登录页
            got = _pick_common_account(ui, target)
            if got is None:
                res.reason = ("网易登录页的「常用」列表里没有 %s —— "
                              "这个账号需要先在这台机器的模拟器里手动登录一次"
                              % target)
                res.steps.append("常用列表未命中")
                return res
            if not got:
                res.steps.append("切账号动作没生效")
                continue

            # 回到游戏登录页，确认账号
            items2, _ = ui.ocr("swacct_verify")
            if guard_survey(ui, items2):
                items2, _ = ui.ocr("swacct_verify2")
            now2 = find_masked(items2)
            if now2 and _same_account(now2, target):
                res.ok, res.swapped, res.masked = True, True, now2
                res.reason = "已切到 %s" % now2
                return res
            res.steps.append("切完读到的账号是 %s，与目标不符" % (now2 or "读不出"))
            continue

        # 不在登录页（可能已经进游戏了）→ 尝试退回登录页的用户中心入口
        res.steps.append("第%d轮：不在登录页，试着找入口" % rnd)
        if not _open_user_center(ui):
            res.reason = "当前不在登录页，也找不到「用户中心」入口"
            res.steps.append("不在登录页")
            return res
        if not _tap_switch_account(ui):
            _close_user_center(ui)
            continue
        got = _pick_common_account(ui, target)
        if got is None:
            res.reason = ("「常用」列表里没有 %s（需先手动登录一次）" % target)
            return res
        if got:
            items2, _ = ui.ocr("swacct_verify3")
            now2 = find_masked(items2)
            if now2 and _same_account(now2, target):
                res.ok, res.swapped, res.masked = True, True, now2
                res.reason = "已切到 %s" % now2
                return res

    if not res.reason:
        res.reason = "重试 %d 轮仍未切成功" % max_rounds
    return res


def _same_account(a: str, b: str) -> bool:
    """脱敏串比较：去掉空格和不可见字符，星号数量不同也算同一个。"""
    na, nb = norm(a).replace(" ", ""), norm(b).replace(" ", "")
    if na == nb:
        return True
    # 星号个数 OCR 常读错（*** vs ****），把星号串归一化成一个 * 再比
    ra = re.sub(r"\*+", "*", na)
    rb = re.sub(r"\*+", "*", nb)
    return ra == rb


def _open_user_center(ui) -> bool:
    """打开用户中心面板。

    ⚠️ **只在标题页 / 登录页上有效**（2026-09-20 实测确认）：
       LOGIN_ACCOUNT_ICON = (60,62) 是**登录页左上角的账号图标**，
       而主城左上角同一个位置是「**任务**」按钮（两者只差 40px）。
       从主城调这个函数会点开「任务」面板然后返回 False。
       需要「从游戏里回登录页」时，请用 `restart_game_to_login()`
       （主城上没有任何回登录页的入口，只能重启游戏）。
    """
    items, _ = ui.ocr("uc_open")
    if _has(items, *KW_USER_CENTER):
        return True
    # 登录页左上角图标；换区后这个入口依然在
    _safe_tap(ui, LOGIN_ACCOUNT_ICON, "登录页左上角（用户中心）", delay=2.0)
    items, _ = ui.ocr("uc_check")
    if guard_survey(ui, items):
        items, _ = ui.ocr("uc_check2")
    return _has(items, *KW_USER_CENTER)


def _tap_switch_account(ui) -> bool:
    """在用户中心里点「切换账号」并在网易登录页出现后返回 True。"""
    items, _ = ui.ocr("uc_switch")
    if guard_survey(ui, items):
        items, _ = ui.ocr("uc_switch2")
    hit = _hit(items, *KW_SWITCH_ACCOUNT)
    if hit is not None:
        _safe_tap(ui, hit.center, "切换账号", delay=2.5)
    else:
        _safe_tap(ui, UC_SWITCH_ACCOUNT, "切换账号（固定坐标）", delay=2.5)

    items, _ = ui.ocr("nn_page")
    # 网易登录页的特征：有「其他账号登录」、有网易 logo、或读到脱敏账号。
    # 不用裸「登录」判定（自动登录/上次登录会误命中）。
    return _has(items, *KW_OTHER_LOGIN) or _has(items, *KW_NETEASE) \
        or _has(items, *KW_COMMON_TAB) or find_masked(items) is not None


def _close_user_center(ui) -> None:
    items, _ = ui.ocr("uc_close")
    hit = _hit(items, "关闭", "✕", "╳")
    if hit is not None and hit.center[0] > 1200:
        ui.tap(*hit.center)
        time.sleep(1.0)
    else:
        ui.tap(*UC_CLOSE)
        time.sleep(1.0)


def _pick_common_account(ui, target: str) -> Optional[bool]:
    """在网易登录页的「常用」列表里找到 target 并点「登录」。

    返回：
      True  —— 点了切换动作
      None  —— 列表里找不到这个账号（能力边界，调用方要如实告知用户）
    只有这两种返回：失败要么是能力边界，要么是动作没生效（由调用方重试）。
    """
    items, _ = ui.ocr("nn_list")
    if guard_survey(ui, items):
        items, _ = ui.ocr("nn_list2")

    # 先找有没有目标脱敏串
    cand: Optional[TextItem] = None
    for it in items:
        mk = mask_of(it.text)
        if mk and _same_account(mk, target):
            cand = it
            break

    if cand is None:
        # 有时账号列表被折叠在「常用」页签里，点一下页签再看
        hit = _hit(items, *KW_COMMON_TAB)
        if hit is not None:
            _safe_tap(ui, hit.center, "常用页签", delay=1.5)
        else:
            _safe_tap(ui, NN_TAB_COMMON, "常用页签（固定坐标）", delay=1.5)
        items, _ = ui.ocr("nn_list3")
        for it in items:
            mk = mask_of(it.text)
            if mk and _same_account(mk, target):
                cand = it
                break

    if cand is None:
        return None                      # 找不到 → 能力边界

    # 点了账号之后再点「登录」
    _safe_tap(ui, cand.center, "常用列表里的 %s" % target, delay=1.2)
    items, _ = ui.ocr("nn_after_pick")
    guard_survey(ui, items)
    login_hit = _find_login_button(items)
    if login_hit is not None:
        _safe_tap(ui, login_hit.center, "登录", delay=3.0)
    else:
        _safe_tap(ui, NN_LOGIN_BTN, "登录（固定坐标）", delay=3.0)
    return True


# ================================================================== 角色切换

# 页签名 -> 兜底坐标（选择服务器面板里的一级页签）
_TAB_POINTS = {
    "已有角色": SRV_TAB_HAVE_ROLE,
    "经典服": SRV_TAB_CLASSIC,
    "青春服": SRV_TAB_YOUTH,
    "最近登录": SRV_SUB_RECENT,
    "经典服角色": SRV_SUB_CLASSIC_ROLE,
}


def switch_role(ui, role: str, *, tab: str = "已有角色", max_rounds: int = 4) -> SwitchResult:
    """在**当前账号内**切到指定角色。

    走登录页「点击换区」→「选择服务器」面板 → 点目标角色 → 确定。不需要密码。

    ★ **定位只认角色名**（见 `_find_role`）：
      区服会随合服改名（今天的 X6014，合服后可能变成别的），拿它当判据迟早失效。
      所以这里不再接受 server / season 参数 —— 它们连"参考加分"都不做。
      找不到就如实说明，绝不瞎点。
    """
    want = (role or "").strip()
    res = SwitchResult(False, "role")
    if not want:
        res.reason = "没给目标角色名"
        return res

    variants = _name_variants(want)

    for rnd in range(1, max_rounds + 1):
        items, _ = ui.ocr("swrole_%d" % rnd)
        if guard_survey(ui, items):
            continue

        # 必须先在登录页，不然「点击换区」这个入口不在
        if not _has(items, *KW_START_GAME) and not _has(items, *KW_AREA_ENTRY):
            # 已经进游戏了：先不动，交给调用方决定要不要退回来
            res.reason = "当前不在登录页（看不到「点击换区」），无法切角色"
            res.steps.append("第%d轮：不在登录页" % rnd)
            return res

        # 打开选择服务器面板
        if not _open_server_panel(ui):
            res.steps.append("第%d轮：打不开「选择服务器」面板" % rnd)
            continue

        # 按页签找角色
        if tab:
            _pick_tab(ui, tab)

        hit, note = _find_role(ui, want)
        if note:
            res.reason = note
            res.steps.append("第%d轮：%s" % (rnd, note))
            _press_confirm(ui)
            return res

        if hit is None:
            # 换个页签再找一遍（角色可能在「经典服」/「青春服」下）
            for t in ("已有角色", "最近登录", "经典服", "青春服"):
                if t == tab:
                    continue
                _pick_tab(ui, t)
                hit, note = _find_role(ui, want)
                if note:
                    res.reason = note
                    res.steps.append("第%d轮：%s" % (rnd, note))
                    _press_confirm(ui)
                    return res
                if hit is not None:
                    res.steps.append("在「%s」页签下找到角色" % t)
                    break

        if hit is None:
            res.reason = ("「选择服务器」面板里找不到角色「%s」—— "
                          "确认登记的角色名和游戏里显示的一致（只按名字找，不看区服）"
                          % want)
            _press_confirm(ui)
            return res

        res.steps.append("找到角色 %s @ %s" % (hit.text[:24], hit.center))
        _safe_tap(ui, hit.center, "角色 %s" % want, delay=1.2)

        # 点确定
        _press_confirm(ui)

        # 校验：回到登录页后应该能看到开始游戏（角色已选好）
        items2, _ = ui.ocr("swrole_verify")
        if guard_survey(ui, items2):
            items2, _ = ui.ocr("swrole_verify2")
        if _has(items2, *KW_START_GAME):
            res.ok, res.swapped, res.role = True, True, want
            res.reason = "已选好角色 %s，可以开始游戏" % want
            return res
        res.steps.append("点了确定但没回到登录页")
        continue

    if not res.reason:
        res.reason = "重试 %d 轮仍未切到角色「%s」" % (max_rounds, want)
    return res


def _open_server_panel(ui) -> bool:
    """点「点击换区」，等「选择服务器」面板出现。"""
    items, _ = ui.ocr("srv_open")
    # 注意：用 _exact 判标题。登录页上的「未选择服务器」含「选择服务器」子串，
    # 用子串匹配会把登录页当成面板已打开，导致后面在登录页上瞎找角色。
    if _exact(items, *KW_SELECT_SERVER):
        return True
    hit = _hit(items, *KW_AREA_ENTRY)
    if hit is not None:
        _safe_tap(ui, hit.center, "点击换区", delay=2.5)
    else:
        _safe_tap(ui, AREA_SWITCH_ENTRY, "点击换区（固定坐标）", delay=2.5)

    items, _ = ui.ocr("srv_check")
    if guard_survey(ui, items):
        items, _ = ui.ocr("srv_check2")
    # 面板特征：标题「选择服务器」，或出现「已有角色」页签
    return _exact(items, *KW_SELECT_SERVER) or _has(items, *KW_ALREADY_ROLE)


def _pick_tab(ui, tab: str) -> None:
    """点选择服务器面板里的页签。点不到就算了 —— 后面还会遍历所有页签找角色。"""
    items, _ = ui.ocr("tab_%s" % tab)
    hit = _hit(items, tab)
    if hit is not None and hit.center[1] < 700:      # 页签都在上半屏
        _safe_tap(ui, hit.center, "页签 %s" % tab, delay=1.2)
        return
    pt = _TAB_POINTS.get(tab)
    if pt:
        _safe_tap(ui, pt, "页签 %s（固定坐标）" % tab, delay=1.2)


# 区服编号的**格式**（X6014 / s12345 / S6014 …）。
# ⚠ 只用来「把前缀切掉」，**从不比较区服的具体值** ——
#   合服后 X6014 可能变成完全不同的编号，比具体值就等于埋了个定时炸弹。
_SRV_PREFIX_RE = re.compile(r"^[A-Za-z]{1,3}\d{3,5}")


def _name_variants(role: str) -> List[str]:
    """角色名的等价写法。

    面板上的角色条目常被 OCR 读成「X6014龙兴之」这种「区服编号 + 名字」连读框，
    用户登记时也可能连区服一起填了。所以生成两种写法：原样、以及去掉开头
    区服编号后的部分。这样「只填名字」和「连区服一起填」都能命中。
    """
    out = [role]
    m = _SRV_PREFIX_RE.match(role)
    if m and len(role) > m.end():
        out.append(role[m.end():])
    return [v.strip() for v in out if v and v.strip()]


def _one_score(text: str, key: str) -> float:
    """条目文本 vs 一个名字写法的匹配分。"""
    t = fix_ocr(norm(text), drop=True)
    k = fix_ocr(norm(key))
    if not t or not k:
        return 0.0
    if t == k:
        return 1.0
    if k in t:
        # 名字只是条目的一部分（面板把区服和名字挤在一个框里）。
        # 单字名字的子串匹配太危险（几乎必然误命中），直接不算。
        if len(k) < 2:
            return 0.0
        return 0.80 + 0.18 * (len(k) / len(t))
    return match_score(text, key)


def _find_role(ui, role: str) -> Tuple[Optional[TextItem], str]:
    """在「选择服务器」面板里按**角色名**找角色。

    返回 `(命中条目 或 None, 需要告知用户的说明)`。说明非空时表示「不敢确定」，
    调用方应把它当失败原因报出来，而**不是**去猜一个点下去。

    ★ 只认角色名：
      区服会随合服改名（今天的 X6014，合服后可能变成别的编号），
      拿区服 / 赛季当判据迟早会在某一天全线失效。名字才是稳定标识。

    匹配从严到宽：
      1. 归一化后完全相等
      2. 条目文本包含角色名（面板常把「区服 + 名字」连读成一个框）
      3. 模糊匹配（match_score ≥ 0.62，带长度约束）

    同名歧义：出现 ≥2 个分数接近（差 < 0.15）的候选时**不猜**，返回说明。
    点错角色 = 在别人的号上跑任务，比不跑糟糕得多。
    """
    items, _ = ui.ocr("findrole")
    guard_survey(ui, items)

    variants = _name_variants(role)
    if not variants:
        return None, "没给目标角色名"

    # 角色条目一般在面板下半屏的列表区
    cands = [it for it in items if 300 < it.center[1] < 880 and it.center[0] > 120]

    scored: List[Tuple[float, TextItem]] = []
    for it in cands:
        sc = max((_one_score(it.text, k) for k in variants), default=0.0)
        if sc > 0:
            scored.append((sc, it))
    scored.sort(key=lambda x: (-x[0], x[1].center[1], x[1].center[0]))

    if not scored or scored[0][0] < 0.62:
        return None, ""
    best_sc, best = scored[0]
    if len(scored) > 1 and scored[1][0] >= 0.62 and (best_sc - scored[1][0]) < 0.15:
        return None, ("面板里有不止一个像「%s」的条目（「%s」和「%s」），"
                      "不敢替你猜是哪一个 —— 请把角色名填得更精确一些"
                      % (role, best.text[:18], scored[1][1].text[:18]))
    return best, ""


def _press_confirm(ui) -> None:
    items, _ = ui.ocr("srv_confirm")
    hit = _hit(items, *KW_CONFIRM)
    if hit is not None and hit.center[1] > 700:      # 确定按钮在底部
        _safe_tap(ui, hit.center, "确定", delay=2.5)
        return
    _safe_tap(ui, SRV_CONFIRM, "确定（固定坐标）", delay=2.5)


# ================================================================== 统一入口

def ensure_target(ui, assignment: Dict[str, Any], *,
                  log: Optional[Callable[[str], None]] = None,
                  dry_run: bool = False) -> SwitchResult:
    """按后端指派，把客户端带到「正确的账号 + 正确的角色」。

    assignment 就是心跳响应里的那个对象：
        {label, masked, role, server, season, tab, ...}

    流程：
      1. 先在登录页读当前账号。和目标一致 → 跳过换账号
      2. 不一致 → 换账号（免密，走「常用」列表）
      3. 有角色要求 → 换角色
      4. 都好了 → 调用方去点「开始游戏」
    """
    say = log or ui.log
    masked = str(assignment.get("masked") or "").strip()
    role = str(assignment.get("role") or "").strip()
    label = str(assignment.get("label") or "").strip()

    if not masked and not role:
        return SwitchResult(True, "none", reason="没指派账号/角色，什么都不用切")

    say("· 目标账号/角色：%s%s"
        % (label or masked or "（未命名）", (" / " + role) if role else ""))

    if dry_run:
        say("  · 预演模式：只识别不切换")
        return SwitchResult(True, "dry", reason="预演模式跳过切换")

    # ---- 账号 ----
    if masked:
        now, _ = current_account(ui)
        if now and _same_account(now, masked):
            say("  ✓ 账号已正确（当前 %s），跳过换账号" % now)
        else:
            say("  · 当前账号 %s ≠ 目标 %s → 开始切换" % (now or "读不出", masked))
            r = switch_account(ui, masked)
            for s in r.steps:
                say("      %s" % s)
            if not r.ok:
                say("  × 换账号失败：%s" % r.reason)
                return r
            say("  ✓ %s" % r.reason)
            # 换完账号，游戏可能已经在下载/加载，等一会儿再继续
            time.sleep(2.0)

    # ---- 角色 ----
    if role:
        # ★ 只传角色名：切换定位不看区服 / 赛季（合服会改名）。
        #   assignment 里虽然还带着 server/season，但那是给人看的备注，不参与判断。
        r2 = switch_role(ui, role,
                         tab=str(assignment.get("tab") or "已有角色"))
        for s in r2.steps:
            say("      %s" % s)
        if not r2.ok:
            say("  × 换角色失败：%s" % r2.reason)
            return r2
        say("  ✓ %s" % r2.reason)

    return SwitchResult(True, "both", reason="账号与角色均已就位",
                        swapped=True, masked=masked, role=role)


# ================================================================== 探测

def describe_screen(ui, limit: int = 24) -> Dict[str, Any]:
    """给「探测当前界面」用：把屏幕上的文字读出来，附带设备状态。

    这就是后台点「探测」按钮时客户端执行的东西，结果原样回传，
    所以后台看到的就是那台机器**此刻**的真实界面文字。
    """
    items, path = ui.ocr("probe")
    guard_survey(ui, items)
    lines: List[str] = []
    for it in items:
        t = (it.text or "").strip()
        if t:
            lines.append("%s  %s" % (t[:46], tuple(it.center)))
        if len(lines) >= limit:
            break

    info: Dict[str, Any] = {
        "lines": lines,
        "text_count": len(items),
        "shot": path,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        info["device"] = {
            "serial": ui.dev.serial,
            "foreground": ui.dev.foreground(),
            "game_running": ui.dev.game_running(
                (ui.cfg or {}).get("device.package") if ui.cfg else None),
        }
    except Exception:
        pass
    return info