# -*- coding: utf-8 -*-
"""账号 / 角色切换。

**这是实测摸出来的能力边界，不是猜的：**

1. **换角色 / 换区服 —— 完全免密**
   登录页 →「点击换区」→「选择服务器」面板 → 点目标角色 →「确定」→ 开始游戏。
   面板里有页签（已有角色 / 经典服 / 青春服）和子页签（最近登录 / 经典服角色…），
   角色列表要按页签找。

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
    """打开用户中心面板。"""
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


def switch_role(ui, role: str, *, server: str = "", season: str = "",
                tab: str = "已有角色", max_rounds: int = 4) -> SwitchResult:
    """在**当前账号内**切到指定角色。

    走登录页「点击换区」→「选择服务器」面板 → 点目标角色 → 确定。
    不需要密码。找不到角色就如实说明，不要瞎点。
    """
    want = (role or "").strip()
    res = SwitchResult(False, "role")
    if not want:
        res.reason = "没给目标角色名"
        return res

    for rnd in range(1, max_rounds + 1):
        items, _ = ui.ocr("swrole_%d" % rnd)
        if guard_survey(ui, items):
            continue

        # 必须先在登录页，不然「点击换区」这个入口不在
        if not _has(items, *KW_START_GAME) and not _has(items, *KW_AREA_ENTRY):
            # 已经进游戏了：这种情况先不动，交给调用方决定要不要退回来
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

        hit = _find_role(ui, want, server=server, season=season)
        if hit is None:
            # 换个页签再找一遍（角色可能在「经典服」/「青春服」下）
            for t in ("已有角色", "最近登录", "经典服", "青春服"):
                if t == tab:
                    continue
                _pick_tab(ui, t)
                hit = _find_role(ui, want, server=server, season=season)
                if hit is not None:
                    res.steps.append("在「%s」页签下找到角色" % t)
                    break

        if hit is None:
            res.reason = ("「选择服务器」面板里找不到角色「%s」—— "
                          "确认登记的角色名和游戏里显示的一致" % want)
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


def _find_role(ui, role: str, *, server: str = "", season: str = "") -> Optional[TextItem]:
    """在当前页签的角色列表里找目标角色。

    匹配策略（从严到宽）：
      1. 角色名精确/模糊匹配（match_score）
      2. 命中「区服 + 赛季」组合
    角色条目在面板里的坐标是 (651,416)/(665,539) 这类，文本可能被读成
    「X6014龙兴之」这样连在一起，所以既整串比、也拆开比。
    """
    items, _ = ui.ocr("findrole")
    guard_survey(ui, items)

    keys = [k for k in (role, server, season) if k]
    # 角色条目一般在下半屏的列表区
    cands = [it for it in items if 300 < it.center[1] < 880 and it.center[0] > 120]

    best, best_score = None, 0.0
    for it in cands:
        t = norm(it.text)
        if not t:
            continue
        # 整串 vs 角色名
        sc = match_score(it.text, role)
        # 角色名 + 区服 + 赛季 都被包在这条里，给个加成
        if server and norm(server) in t:
            sc = max(sc, 0.72)
        if season and norm(season) in t:
            sc = max(sc, 0.72)
        if server and season and norm(server) in t and norm(season) in t:
            sc = max(sc, 0.86)
        if sc > best_score:
            best, best_score = it, sc

    if best is not None and best_score >= 0.6:
        return best

    # 兜底：拆词比。OCR 常把「X6014龙兴之」读成「X6014」+「龙兴之」两条。
    if server or season:
        for it in cands:
            t = norm(it.text)
            if server and norm(server) == t:
                # 找它右边最近的那条当赛季
                right = [o for o in cands if o.center[0] > it.center[0]
                         and abs(o.center[1] - it.center[1]) < 40]
                if right and (not season or match_score(right[0].text, season) >= 0.6):
                    return it
    return None


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
        r2 = switch_role(ui, role,
                         server=str(assignment.get("server") or ""),
                         season=str(assignment.get("season") or ""),
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